#!/usr/bin/env bash
# Unified version bumper for PCAI repos.
#
# Detects and bumps every Helm chart under <root>/helm*/ plus, when present,
# the `version = "..."` field in <root>/pyproject.toml.  Per-repo conventions
# (chart dir names like helm-scale/helm-scale-g2 -> "-scale"/"-g2" suffixes,
# quoted vs unquoted tags, `v` prefix in appVersion) are detected from the
# existing files, so the same script works everywhere.
#
# Usage: ./bump_version.sh <version> [<target-repo-root>]
#   <version>            new version, e.g. 1.9.0
#   <target-repo-root>   defaults to this script's directory, so the script
#                        can be hardlinked into a repo root and run as
#                        ./bump_version.sh 1.9.0
set -uo pipefail

VERSION="${1:?Usage: $0 <version> (e.g. 1.9.0)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -eq 2 ]]; then
  ROOT="$(cd "$2" && pwd)"
fi

if [[ ! -d "$ROOT" ]]; then
  echo "ERROR: '$ROOT' is not a directory" >&2
  exit 1
fi

shopt -s nullglob
charts=("$ROOT"/helm*/Chart.yaml)
shopt -u nullglob

updated=0
for chart_yaml in "${charts[@]}"; do
  chart_dir="$(dirname "$chart_yaml")"
  chart_name="$(basename "$chart_dir")"
  values_yaml="$chart_dir/values.yaml"

  # Derive the chart's suffix from its own current version string, e.g.
  # "1.9.0" -> "" , "1.9.0-scale" -> "-scale", "1.9.0-g2" -> "-g2".
  # This is robust to directory naming (helm, helm-scale, helm-scale-g2).
  current_version="$(sed -nE 's/^version:[[:space:]]*([^[:space:]]*).*/\1/p' "$chart_yaml" | head -1)"
  suffix="$(sed -nE 's/^[0-9]+(\.[0-9]+)*//p' <<<"$current_version")"

  if [[ ! -f "$values_yaml" ]]; then
    echo "SKIP  $chart_dir (no values.yaml)" >&2
    continue
  fi

  # Detect existing appVersion style: keep the "v" prefix if already present.
  if grep -qE '^appVersion:\s*"v' "$chart_yaml"; then
    app_line="appVersion: \"v${VERSION}\""
  else
    app_line="appVersion: \"${VERSION}\""
  fi

  sed -i "s/^version: .*/version: ${VERSION}${suffix}/" "$chart_yaml"
  sed -i "s/^appVersion: .*/${app_line}/" "$chart_yaml"

  # Detect tag style in values.yaml: quoted vs unquoted.  Only the
  # top-level image tag (exactly 2-space indented, like "  tag: v...")
  # is touched; nested tags (e.g. sidecar images) are left alone.
  if grep -qE '^  tag: "v' "$values_yaml"; then
    sed -i 's/^\(  tag: "\)v.*"/\1v'"${VERSION}"'"/' "$values_yaml"
  else
    sed -i 's/^\(  tag: \)v.*/\1v'"${VERSION}"'/' "$values_yaml"
  fi

  echo "Updated $chart_name → ${VERSION}${suffix} (appVersion ${VERSION})"
  updated=1
done

if [[ -f "$ROOT/pyproject.toml" ]] && grep -qE '^version = "?[0-9]' "$ROOT/pyproject.toml"; then
  sed -i "s/^\(version = \)\"[^\"]*\"/\1\"${VERSION}\"/" "$ROOT/pyproject.toml"
  echo "Updated pyproject.toml → ${VERSION}"
  updated=1
fi

# Python package source version (single source of truth for __version__ /
# the MCP handshake / /api/status). Without this, source strings drift from
# pyproject.toml on every release.
shopt -s nullglob
init_files=("$ROOT"/src/*/__init__.py)
shopt -u nullglob
for init_file in "${init_files[@]}"; do
  if grep -qE '^__version__ = ".' "$init_file"; then
    sed -i "s/^__version__ = \"[^\"]*\"/__version__ = \"${VERSION}\"/" "$init_file"
    echo "Updated ${init_file#"$ROOT"/} → ${VERSION}"
    updated=1
  fi
done

if [[ $updated -eq 0 ]]; then
  echo "No Helm charts or pyproject.toml version found under $ROOT" >&2
  exit 1
fi
echo "Done."