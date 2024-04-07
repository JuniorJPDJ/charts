#!/bin/bash
set -eu
parent_dir="$1"

ver=$(grep "^version:" "charts/${parent_dir}/Chart.yaml" | awk '{print $2}')

if [ -z "$ver" ]; then
  echo "No valid version was found"
  exit 1
fi

# bump patch level of the version
# will not work with rc and other suffixes
bumped_ver="${ver%.*}.$(( ${ver##*.} + 1 ))"

echo "Bumping version for $parent_dir from $ver to $bumped_ver"
sed -i "s/^version:.*/version: ${bumped_ver}/g" "charts/${parent_dir}/Chart.yaml"
