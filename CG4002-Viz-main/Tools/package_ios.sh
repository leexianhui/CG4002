#!/bin/zsh
# Package the most recent Xcode device build as an .ipa and record its SHA-256.
#
# Usage:  Tools/package_ios.sh            package + hash
#         Tools/package_ios.sh --install  also install that exact .ipa on the connected iPhone
#
# Run it once, on the final build. Every rebuild or re-package produces a different
# hash (signing and zip timestamps), so submit the hash of the file you install and demo.
set -euo pipefail

app=$(ls -dt ~/Library/Developer/Xcode/DerivedData/Unity-iPhone-*/Build/Products/*-iphoneos/CG4002-Viz.app 2>/dev/null | head -1)
[[ -n "$app" ]] || { echo "No device build found. Run the app from Xcode (Cmd+R) first."; exit 1; }

codesign --verify --deep --strict "$app" || { echo "Signature invalid: rebuild in Xcode."; exit 1; }

out_dir="$(cd "$(dirname "$0")/.." && pwd)/Builds/Release"
stamp=$(date +%Y%m%d-%H%M)
ipa="$out_dir/CG4002-Viz-$stamp.ipa"
work=$(mktemp -d)
mkdir -p "$out_dir" "$work/Payload"
cp -R "$app" "$work/Payload/"
(cd "$work" && zip -qry "$ipa" Payload)
rm -rf "$work"

hash=$(shasum -a 256 "$ipa" | awk '{print $1}')
echo "$hash  $(basename "$ipa")" > "$ipa.sha256"
expiry=$(security cms -D -i "$app/embedded.mobileprovision" | plutil -extract ExpirationDate raw -)

echo "Built from : $app"
echo "Signed     : $(codesign -dv "$app" 2>&1 | sed -n 's/^Signed Time=//p')"
echo "Expires    : $expiry   <- app stops launching after this; the demo must be before it"
echo "Package    : $ipa"
echo "SHA-256    : $hash"

if [[ "${1:-}" == "--install" ]]; then
  device=$(xcrun devicectl list devices 2>/dev/null | awk '/iPhone/ && /(connected|available)/ && !/unavailable/ {print $3; exit}')
  [[ -n "$device" ]] || { echo "No available iPhone. Connect and unlock it, then install $ipa manually."; exit 1; }
  xcrun devicectl device install app --device "$device" "$ipa"
  echo "Installed the hashed package on $device."
fi
