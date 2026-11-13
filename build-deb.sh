#!/bin/sh
# Build pipequi_<version>_all.deb from this checkout: ./build-deb.sh 0.1.0
set -eu
version=${1:?usage: ./build-deb.sh VERSION}
here=$(cd "$(dirname "$0")" && pwd)
root=$(mktemp -d)
chmod 755 "$root"
trap 'rm -rf "$root"' EXIT

install -Dm755 "$here/pipequi.py" "$root/usr/bin/pipequi"
install -Dm644 "$here/pipequi.desktop" "$root/usr/share/applications/pipequi.desktop"
install -Dm644 "$here/README.md" "$root/usr/share/doc/pipequi/README.md"
install -Dm644 "$here/LICENSE" "$root/usr/share/doc/pipequi/copyright"

mkdir -p "$root/DEBIAN"
cat > "$root/DEBIAN/control" <<EOF
Package: pipequi
Version: $version
Architecture: all
Maintainer: kritzerenkrieg <kritzerenkrieg@users.noreply.github.com>
Installed-Size: $(du -sk "$root/usr" | cut -f1)
Depends: python3 (>= 3.8), python3-gi, gir1.2-gtk-3.0, pipewire-bin, wireplumber
Recommends: bluez
Section: sound
Priority: optional
Homepage: https://pipequi.kuritzu.de
Description: native PipeWire equalizer GUI
 A parametric equalizer that runs inside PipeWire itself as a filter-chain,
 with no background process. The window edits the running filter live and
 saves it for the next boot; Set up and Remove are one click each.
EOF

dpkg-deb --root-owner-group --build "$root" "$here/pipequi_${version}_all.deb"
