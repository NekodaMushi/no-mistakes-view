#!/usr/bin/env sh
# Pose un lien `no-mistakes-view` dans ~/.local/bin (ou dans le dossier donné).
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
dest=${1:-"$HOME/.local/bin"}
mkdir -p "$dest"
ln -sfn "$here/bin/no-mistakes-view" "$dest/no-mistakes-view"
echo "Lien créé : $dest/no-mistakes-view -> $here/bin/no-mistakes-view"
case ":$PATH:" in
  *":$dest:"*) ;;
  *) echo "Attention : $dest n'est pas dans le PATH." ;;
esac
