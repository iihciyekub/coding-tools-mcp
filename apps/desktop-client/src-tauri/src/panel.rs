use tauri::AppHandle;
#[cfg(not(target_os = "macos"))]
use tauri::Manager;

#[derive(Clone, Copy)]
pub enum PanelEvent {
    TrayClick,
    FocusLost { pointer_over_tray: bool },
}

pub fn visibility_after_event(visible: bool, event: PanelEvent) -> bool {
    match event {
        PanelEvent::TrayClick => !visible,
        // A tray press can blur the window before its mouse-up click arrives.
        // Leave visibility to that click so it can close an already-open panel.
        PanelEvent::FocusLost { pointer_over_tray } => visible && pointer_over_tray,
    }
}

fn contains_point(point: (f64, f64), origin: (f64, f64), size: (f64, f64)) -> bool {
    point.0 >= origin.0
        && point.0 < origin.0 + size.0
        && point.1 >= origin.1
        && point.1 < origin.1 + size.1
}

#[cfg(target_os = "macos")]
pub fn pointer_over_tray(app: &AppHandle, tray_id: &str) -> bool {
    let Some(tray) = app.tray_by_id(tray_id) else {
        return false;
    };
    tray.with_inner_tray_icon(|icon| {
        let Some(mtm) = objc2_foundation::MainThreadMarker::new() else {
            return false;
        };
        let Some(window) = icon
            .ns_status_item()
            .and_then(|item| item.button(mtm))
            .and_then(|button| button.window())
        else {
            return false;
        };
        // Compare native screen points in the same coordinate system. Tauri's
        // cursor and tray pixels use different scales on mixed-DPI displays.
        let point = objc2_app_kit::NSEvent::mouseLocation();
        let frame = window.frame();
        contains_point(
            (point.x, point.y),
            (frame.origin.x, frame.origin.y),
            (frame.size.width, frame.size.height),
        )
    })
    .unwrap_or(false)
}

#[cfg(not(target_os = "macos"))]
pub fn pointer_over_tray(app: &AppHandle, tray_id: &str) -> bool {
    let Some(tray) = app.tray_by_id(tray_id) else {
        return false;
    };
    let (Ok(Some(rect)), Ok(point)) = (tray.rect(), app.cursor_position()) else {
        return false;
    };
    let scale_factor = app
        .get_webview_window("main")
        .and_then(|window| window.scale_factor().ok())
        .unwrap_or(1.0);
    let origin = rect.position.to_physical::<f64>(scale_factor);
    let size = rect.size.to_physical::<f64>(scale_factor);
    contains_point(
        (point.x, point.y),
        (origin.x, origin.y),
        (size.width, size.height),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tray_click_toggles_and_blur_cannot_reopen_or_close_it() {
        let tray_blur = PanelEvent::FocusLost {
            pointer_over_tray: true,
        };
        let mut visible = false;
        visible = visibility_after_event(visible, tray_blur);
        visible = visibility_after_event(visible, PanelEvent::TrayClick);
        assert!(visible);
        // Blur may arrive either before or after the click callback.
        visible = visibility_after_event(visible, tray_blur);
        assert!(visible);
        visible = visibility_after_event(visible, tray_blur);
        visible = visibility_after_event(visible, PanelEvent::TrayClick);
        assert!(!visible);
        visible = visibility_after_event(visible, tray_blur);
        assert!(!visible);
        assert!(visibility_after_event(visible, PanelEvent::TrayClick));
    }

    #[test]
    fn outside_blur_hides_and_the_next_tray_click_reopens() {
        let visible = visibility_after_event(
            true,
            PanelEvent::FocusLost {
                pointer_over_tray: false,
            },
        );
        assert!(!visible);
        assert!(visibility_after_event(visible, PanelEvent::TrayClick));
    }

    #[test]
    fn tray_hit_test_supports_negative_screen_origins_and_excludes_neighbors() {
        let origin = (-180.0, 880.0);
        let size = (24.0, 24.0);
        assert!(contains_point((-180.0, 880.0), origin, size));
        assert!(contains_point((-156.5, 903.5), origin, size));
        assert!(!contains_point((-180.5, 890.0), origin, size));
        assert!(!contains_point((-156.0, 890.0), origin, size));
        assert!(!contains_point((-170.0, 904.0), origin, size));
    }
}
