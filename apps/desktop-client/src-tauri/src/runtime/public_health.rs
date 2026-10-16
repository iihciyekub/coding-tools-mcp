//! Verify the public Gateway even while a local/VPN resolver caches NXDOMAIN.
use super::*;
use std::net::Ipv4Addr;

#[derive(Debug, PartialEq)]
pub(super) enum PublicHealth {
    Healthy,
    DnsUnavailable,
    Unreachable,
}

impl PublicHealth {
    pub(super) fn error(&self) -> Option<&'static str> {
        match self {
            Self::Healthy => None,
            Self::DnsUnavailable => {
                Some("Domain lookup failed; check DNS or wait for it to update")
            }
            Self::Unreachable => {
                Some("Public endpoint is unreachable; checking again automatically")
            }
        }
    }
}

struct Reply {
    code: i32,
    body: Vec<u8>,
}

pub(super) fn public_card_matches(raw: &[u8], name: &str) -> bool {
    serde_json::from_slice::<serde_json::Value>(raw).is_ok_and(|card| {
        card["server"]["name"].as_str() == Some(name)
            && card["transport"]["endpoint"].as_str() == Some(MCP_ENDPOINT_PATH)
    })
}

pub(super) fn probe(origin: &str, name: &str) -> PublicHealth {
    let deadline = Instant::now() + Duration::from_secs(5);
    probe_with(origin, name, |url, resolve| {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining < Duration::from_millis(50) {
            return None;
        }
        // All attempts share a five-second budget. Keep ordinary TLS/hostname
        // verification, including when --resolve supplies a fresh DNS answer.
        let limit = format!("{:.3}", remaining.as_secs_f64().min(3.0));
        let mut command = Command::new("/usr/bin/curl");
        command.args([
            "--disable",
            "--silent",
            "--fail",
            "--connect-timeout",
            "2",
            "--max-time",
            &limit,
            "--max-filesize",
            "65536",
            "--proto",
            "=https",
        ]);
        if let Some(resolve) = resolve {
            command.arg("--resolve").arg(resolve);
        }
        if url.starts_with("https://cloudflare-dns.com/dns-query?") {
            command.args(["--header", "Accept: application/dns-json"]);
        }
        let response = command
            .arg(url)
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
            .ok()?;
        Some(Reply {
            code: response.status.code().unwrap_or(-1),
            body: response.stdout,
        })
    })
}

fn probe_with(
    origin: &str,
    name: &str,
    mut fetch: impl FnMut(&str, Option<&str>) -> Option<Reply>,
) -> PublicHealth {
    let endpoint = format!("{}/.well-known/mcp.json", origin.trim_end_matches('/'));
    let Some(response) = fetch(&endpoint, None) else {
        return PublicHealth::Unreachable;
    };
    if response.code == 0 {
        return if public_card_matches(&response.body, name) {
            PublicHealth::Healthy
        } else {
            PublicHealth::Unreachable
        };
    }
    // Only a resolver failure permits DNS fallback. Authentication, TLS,
    // routing and wrong-server errors must never be hidden by another lookup.
    if response.code != 6 {
        return PublicHealth::Unreachable;
    }
    let Ok(url) = tauri::Url::parse(origin) else {
        return PublicHealth::DnsUnavailable;
    };
    let Some(host) = url.domain() else {
        return PublicHealth::DnsUnavailable;
    };
    let mut dns_url = tauri::Url::parse("https://cloudflare-dns.com/dns-query").unwrap();
    dns_url
        .query_pairs_mut()
        .append_pair("name", host)
        .append_pair("type", "A");
    // Request JSON DNS without a separate HTTP client or a change to the
    // Mac's resolver/VPN settings.
    let Some(dns) = fetch(dns_url.as_str(), None) else {
        return PublicHealth::DnsUnavailable;
    };
    if dns.code != 0 {
        return PublicHealth::DnsUnavailable;
    }
    let addresses = public_addresses(&dns.body);
    if addresses.is_empty() {
        return PublicHealth::DnsUnavailable;
    }
    for ip in addresses {
        let resolve = format!("{host}:{}:{ip}", url.port_or_known_default().unwrap_or(443));
        if fetch(&endpoint, Some(&resolve))
            .is_some_and(|reply| reply.code == 0 && public_card_matches(&reply.body, name))
        {
            return PublicHealth::Healthy;
        }
    }
    PublicHealth::Unreachable
}

fn public_addresses(raw: &[u8]) -> Vec<Ipv4Addr> {
    let Ok(dns) = serde_json::from_slice::<serde_json::Value>(raw) else {
        return vec![];
    };
    if dns["Status"].as_u64() != Some(0) {
        return vec![];
    }
    let mut addresses = Vec::new();
    for answer in dns["Answer"].as_array().into_iter().flatten() {
        if answer["type"].as_u64() != Some(1) {
            continue;
        }
        let Some(ip) = answer["data"]
            .as_str()
            .and_then(|ip| ip.parse::<Ipv4Addr>().ok())
        else {
            continue;
        };
        if ip.is_private()
            || ip.is_loopback()
            || ip.is_link_local()
            || ip.is_documentation()
            || ip.octets()[0] == 0
            || ip.octets()[0] >= 224
            || addresses.contains(&ip)
        {
            continue;
        }
        addresses.push(ip);
        if addresses.len() == 2 {
            break;
        }
    }
    addresses
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[ignore = "requires an explicitly selected live public Gateway and expected server name"]
    fn live_public_gateway_probe() {
        let origin = std::env::var("CODING_TOOLS_MCP_PROBE_ORIGIN").unwrap();
        let name = std::env::var("CODING_TOOLS_MCP_PROBE_SERVER_NAME").unwrap();
        let started = Instant::now();
        assert_eq!(probe(&origin, &name), PublicHealth::Healthy);
        println!("Live public Gateway verified in {:?}", started.elapsed());
        assert!(started.elapsed() < Duration::from_secs(6));
    }

    const CARD: &[u8] = br#"{"server":{"name":"expected"},"transport":{"endpoint":"/mcp"}}"#;
    const DNS: &[u8] = br#"{"Status":0,"Answer":[{"type":1,"data":"104.21.2.52"},{"type":1,"data":"172.67.128.185"}]}"#;

    fn reply(code: i32, body: &[u8]) -> Option<Reply> {
        Some(Reply {
            code,
            body: body.to_vec(),
        })
    }

    #[test]
    fn ordinary_success_does_not_query_another_resolver() {
        let mut count = 0;
        assert_eq!(
            probe_with("https://mcp.example.com", "expected", |_, resolve| {
                count += 1;
                assert!(resolve.is_none());
                reply(0, CARD)
            }),
            PublicHealth::Healthy
        );
        assert_eq!(count, 1);
    }

    #[test]
    fn stale_local_dns_can_be_rechecked_without_changing_the_origin() {
        let mut count = 0;
        assert_eq!(
            probe_with(
                "https://mcp.example.com:8443",
                "expected",
                |url, resolve| {
                    count += 1;
                    match count {
                        1 => {
                            assert!(resolve.is_none());
                            reply(6, b"")
                        }
                        2 => {
                            assert!(url.starts_with("https://cloudflare-dns.com/dns-query?"));
                            reply(0, DNS)
                        }
                        3 => {
                            assert_eq!(url, "https://mcp.example.com:8443/.well-known/mcp.json");
                            assert_eq!(resolve, Some("mcp.example.com:8443:104.21.2.52"));
                            reply(0, CARD)
                        }
                        _ => panic!("unexpected request"),
                    }
                }
            ),
            PublicHealth::Healthy
        );
    }

    #[test]
    fn fallback_still_requires_the_same_gateway() {
        assert_eq!(
            probe_with("https://mcp.example.com", "other-server", |url, resolve| {
                if url.contains("dns-query?") {
                    reply(0, DNS)
                } else if resolve.is_none() {
                    reply(6, b"")
                } else {
                    reply(0, CARD)
                }
            }),
            PublicHealth::Unreachable
        );
    }

    #[test]
    fn tls_http_and_wrong_server_errors_do_not_trigger_dns_fallback() {
        for code in [0, 22, 28, 35, 60] {
            let mut count = 0;
            assert_eq!(
                probe_with("https://mcp.example.com", "expected", |_, _| {
                    count += 1;
                    reply(code, b"<html>error</html>")
                }),
                PublicHealth::Unreachable
            );
            assert_eq!(count, 1);
        }
    }

    #[test]
    fn dns_answers_are_bounded_and_require_public_ip_addresses() {
        assert_eq!(public_addresses(DNS).len(), 2);
        for raw in [b"{}".as_slice(), br#"{"Status":3,"Answer":[{"type":1,"data":"104.21.2.52"}]}"#,
            br#"{"Status":0,"Answer":[{"type":1,"data":"127.0.0.1"},{"type":1,"data":"10.0.0.1"},{"type":1,"data":"192.168.1.1"},{"type":5,"data":"104.21.2.52"}]}"#] {
            assert!(public_addresses(raw).is_empty());
        }
        assert_eq!(
            probe_with("https://mcp.example.com", "expected", |url, _| {
                if url.contains("dns-query?") {
                    reply(0, b"{\"Status\":3}")
                } else {
                    reply(6, b"")
                }
            }),
            PublicHealth::DnsUnavailable
        );
    }
}
