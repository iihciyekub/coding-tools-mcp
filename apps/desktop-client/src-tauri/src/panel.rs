use tauri::AppHandle;
#[cfg(not(target_os = "macos"))]
use tauri::Manager;

pub const PANEL_WIDTH: f64 = 420.0;
pub const PANEL_MAX_HEIGHT: f64 = 544.0;

/// Inputs and output use physical screen pixels, including negative monitor origins.
pub fn panel_bounds(
    tray: (f64, f64, f64, f64),
    work: (f64, f64, f64, f64),
    desired_size: (f64, f64),
    scale: f64,
) -> tauri::PhysicalRect<i32, u32> {
    let margin_x = (8.0 * scale).min(((work.2 - 1.0) / 2.0).max(0.0));
    let margin_y = (8.0 * scale).min(((work.3 - 1.0) / 2.0).max(0.0));
    let width = desired_size
        .0
        .min((work.2 - 2.0 * margin_x).floor())
        .max(1.0);
    let height = desired_size
        .1
        .min((work.3 - 2.0 * margin_y).floor())
        .max(1.0);
    let center = (tray.0 + tray.2 / 2.0, tray.1 + tray.3 / 2.0);
    let distances = [
        (center.1 - work.1).abs(),
        (center.1 - work.1 - work.3).abs(),
        (center.0 - work.0).abs(),
        (center.0 - work.0 - work.2).abs(),
    ];
    let edge = (0..4)
        .min_by(|a, b| distances[*a].total_cmp(&distances[*b]))
        .unwrap();
    let gap = 6.0 * scale;
    let (x, y) = match edge {
        0 => (center.0 - width / 2.0, tray.1 + tray.3 + gap),
        1 => (center.0 - width / 2.0, tray.1 - height - gap),
        2 => (tray.0 + tray.2 + gap, center.1 - height / 2.0),
        _ => (tray.0 - width - gap, center.1 - height / 2.0),
    };
    let min_x = work.0 + margin_x;
    let min_y = work.1 + margin_y;
    tauri::PhysicalRect {
        position: tauri::PhysicalPosition::new(
            x.clamp(min_x, (work.0 + work.2 - margin_x - width).max(min_x))
                .round() as i32,
            y.clamp(min_y, (work.1 + work.3 - margin_y - height).max(min_y))
                .round() as i32,
        ),
        size: tauri::PhysicalSize::new(width as u32, height as u32),
    }
}

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
    fn bottom_taskbar_keeps_growing_pages_above_the_work_area_edge() {
        let tray = (1840.0, 1040.0, 24.0, 24.0);
        let work = (0.0, 0.0, 1920.0, 1032.0);
        let short = panel_bounds(tray, work, (420.0, 200.0), 1.0);
        let tall = panel_bounds(tray, work, (420.0, 544.0), 1.0);
        assert_eq!(short.position.y + short.size.height as i32, 1024);
        assert_eq!(tall.position.y + tall.size.height as i32, 1024);
        assert!(tall.position.x + tall.size.width as i32 <= 1912);
    }

    #[test]
    fn top_and_side_taskbars_open_toward_the_work_area() {
        let top = panel_bounds(
            (900.0, 0.0, 24.0, 24.0),
            (0.0, 32.0, 1920.0, 1048.0),
            (420.0, 400.0),
            1.0,
        );
        assert!(top.position.y >= 40);
        let left = panel_bounds(
            (0.0, 500.0, 40.0, 24.0),
            (48.0, 0.0, 1872.0, 1080.0),
            (420.0, 400.0),
            1.0,
        );
        assert!(left.position.x >= 56);
        let right = panel_bounds(
            (1880.0, 500.0, 40.0, 24.0),
            (0.0, 0.0, 1872.0, 1080.0),
            (420.0, 400.0),
            1.0,
        );
        assert!(right.position.x + right.size.width as i32 <= 1864);
    }

    #[test]
    fn high_dpi_and_negative_monitor_origins_stay_inside_small_work_areas() {
        for scale in [1.0, 1.25, 1.5, 2.0] {
            let bounds = panel_bounds(
                (-80.0, 740.0, 32.0, 32.0),
                (-1280.0, -100.0, 1280.0, 820.0),
                (420.0 * scale, 544.0 * scale),
                scale,
            );
            assert!(bounds.position.x >= -1280);
            assert!(bounds.position.y >= -100);
            assert!(bounds.position.x + bounds.size.width as i32 <= 0);
            assert!(bounds.position.y + bounds.size.height as i32 <= 720);
        }
    }

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
