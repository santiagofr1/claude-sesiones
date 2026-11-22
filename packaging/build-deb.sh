#!/bin/bash
# Uso: packaging/build-deb.sh VERSION [OUTDIR]
# Arma OUTDIR/claude-sesiones_all.deb (default OUTDIR=dist).
set -eu

VERSION="${1:?uso: $0 VERSION [OUTDIR]}"
OUTDIR="${2:-dist}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
chmod 0755 "$STAGE"

install -d "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/share/doc/claude-sesiones"

# la versión se inyecta en la copia, no en el original
sed "s/^VERSION = \"dev\"\$/VERSION = \"$VERSION\"/" "$ROOT/ses.py" > "$STAGE/usr/bin/ses"
chmod 0755 "$STAGE/usr/bin/ses"
install -m 0644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/claude-sesiones/copyright"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: claude-sesiones
Version: $VERSION
Architecture: all
Depends: python3 (>= 3.8)
Section: utils
Priority: optional
Maintainer: santiagofr1 <santiagofr1@users.noreply.github.com>
Homepage: https://github.com/santiagofr1/claude-sesiones
Description: Gestor de sesiones de Claude Code
 Lista, busca, borra a una papelera y restaura sesiones de Claude Code; limpia sesiones vacías, viejas y carpetas .claude-flow.
EOF

mkdir -p "$OUTDIR"
dpkg-deb --build --root-owner-group "$STAGE" "$OUTDIR/claude-sesiones_all.deb"
