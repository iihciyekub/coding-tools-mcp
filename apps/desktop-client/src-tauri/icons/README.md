# App artwork

`app-icon.png` and the 32/64/128/256-pixel images retain the supplied IconKitchen
exports. Other platform sizes are generated with Tauri's icon command.

`AppIcon.icon` is the macOS appearance source. Its vector foreground traces the
supplied monochrome mark and shares one position and scale across appearances:
white background / black mark in light mode, black background / white mark in
dark mode. The tinted appearance also keeps a white foreground. Background and
foreground are separate layers so macOS does not infer a dark icon from a flat
light image and leave a black mark on a black background.

`macos/Assets.car` contains the compiled appearances. The macOS Tauri overlay
bundles it at `Contents/Resources/Assets.car` and sets `CFBundleIconName=AppIcon`.
`icon.icns` is the aligned light fallback for older macOS versions. Both generated
files are committed so the release runner does not need Icon Composer. To update
them after editing the source, use Xcode 26 or newer on macOS:

```sh
npm run build:icons:macos
```

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
