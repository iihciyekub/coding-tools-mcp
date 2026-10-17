import { execFileSync } from "node:child_process";
import { copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

// Check in the compiled catalog so release runners and contributors with older
// Xcode versions package exactly the same light/dark artwork.
const icons = fileURLToPath(new URL("./src-tauri/icons/", import.meta.url));
const output = mkdtempSync(join(tmpdir(), "coding-tools-app-icons-"));
try {
  execFileSync("xcrun", [
    "actool", join(icons, "AppIcon.icon"),
    "--compile", output,
    "--platform", "macosx",
    "--minimum-deployment-target", "10.13",
    "--target-device", "mac",
    "--app-icon", "AppIcon",
    "--standalone-icon-behavior", "all",
    "--output-partial-info-plist", join(output, "Info.plist"),
    "--output-format", "human-readable-text",
  ], { stdio: "inherit" });
  const catalog = JSON.parse(execFileSync("xcrun", [
    "assetutil", "--info", join(output, "Assets.car"),
  ], { encoding: "utf8" }));
  for (const appearance of ["NSAppearanceNameAqua", "NSAppearanceNameDarkAqua", "ISAppearanceTintable"]) {
    if (!catalog.some(asset => asset.Name === "AppIcon" &&
        asset.AssetType === "IconImageStack" && asset.Appearance === appearance)) {
      throw new Error(`Compiled AppIcon is missing ${appearance}`);
    }
  }
  copyFileSync(join(output, "Assets.car"), join(icons, "macos/Assets.car"));
  copyFileSync(join(output, "AppIcon.icns"), join(icons, "icon.icns"));
} finally {
  rmSync(output, { recursive: true, force: true });
}
