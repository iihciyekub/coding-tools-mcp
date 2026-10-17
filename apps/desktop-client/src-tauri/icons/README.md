# App artwork

`app-icon.png` is the supplied IconKitchen macOS master. `icon.icns` and the
32/64/128/256-pixel macOS images retain the corresponding original exports.
Other platform sizes are generated with Tauri's icon command.

`tray-template.svg` embeds the supplied Android monochrome foreground and crops
its transparent margins, leaving padding for an 18-point menu-bar icon.
`tray-template.png` is its 36-pixel render for Retina displays. The panel header
uses the same SVG as a mask. macOS template tinting and the header's text color
adapt the mark to light and dark backgrounds; other platforms use the app icon.

To regenerate the tray PNG from `apps/desktop-client`:

```sh
npm run tauri -- icon src-tauri/icons/tray-template.svg --png 36 --output /tmp/coding-tools-tray
cp /tmp/coding-tools-tray/36x36.png src-tauri/icons/tray-template.png
```
