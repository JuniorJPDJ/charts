#!/bin/bash
set -euo pipefail

# Auto-detect repository info from current git repo
REPO_OWNER="${REPO_OWNER:-$(gh repo view --json owner --jq '.owner.login')}"
REPO_NAME="${REPO_NAME:-$(gh repo view --json name --jq '.name')}"
OUTPUT_DIR="${OUTPUT_DIR:-./pages}"

mkdir -p "$OUTPUT_DIR"
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

# Auto-load GITHUB_TOKEN from gh CLI
GITHUB_TOKEN="${GITHUB_TOKEN:-$(gh auth token 2>/dev/null)}" || {
  echo "Error: Could not get GITHUB_TOKEN. Run: gh auth login"
  exit 1
}

# Get packages belonging to this specific repository
echo "Fetching packages for $REPO_OWNER/$REPO_NAME..."

CHARTS=$(gh api --paginate "users/$REPO_OWNER/packages?package_type=container&per_page=100" \
  --jq ".[] | select(.repository.full_name == \"$REPO_OWNER/$REPO_NAME\") | .name" 2>/dev/null || \
  gh api --paginate "orgs/$REPO_OWNER/packages?package_type=container&per_page=100" \
  --jq ".[] | select(.repository.full_name == \"$REPO_OWNER/$REPO_NAME\") | .name" 2>/dev/null || true)

if [ -z "$CHARTS" ]; then
  echo "No packages found for this repository. Ensure GITHUB_TOKEN has read:packages scope."
  echo "Run: gh auth refresh -s read:packages"
  exit 1
fi

echo "Found charts:"
echo "$CHARTS"

echo "Logging into ghcr.io..."
echo "$GITHUB_TOKEN" | helm registry login ghcr.io -u "$(gh api user --jq '.login')" --password-stdin 2>/dev/null || true

# Store chart version dates for updating index.yaml
declare -A CHART_DATES

# Download all chart versions
for chart in $CHARTS; do
  echo "  Processing chart: $chart"

  # Get all versions for this package with creation timestamps (try user then org API)
  ENCODED_CHART="${chart//\//%2F}"
  VERSIONS_JSON=$(gh api --paginate "users/$REPO_OWNER/packages/container/$ENCODED_CHART/versions?per_page=100" \
    --jq '[.[] | {tag: .metadata.container.tags[], created: .created_at}]' 2>/dev/null || \
    gh api --paginate "orgs/$REPO_OWNER/packages/container/$ENCODED_CHART/versions?per_page=100" \
    --jq '[.[] | {tag: .metadata.container.tags[], created: .created_at}]' 2>/dev/null || echo '[]')

  # Extract unique versions, skip PR versions (Helm converts + to _ in OCI tags)
  VERSIONS=$(echo "$VERSIONS_JSON" | jq -r '.[].tag' | grep -v '_pr-' | sort -Vru) || VERSIONS=""

  # Build version -> date lookup
  declare -A VERSION_DATES
  while read -r tag created; do
    VERSION_DATES["$tag"]="$created"
  done < <(echo "$VERSIONS_JSON" | jq -r '.[] | "\(.tag) \(.created)"')

  if [ -z "$VERSIONS" ]; then
    echo "  No non-PR versions found for $chart"
    continue
  fi

  for version in $VERSIONS; do
    # Store date for this chart version
    CHART_DATES["$chart:$version"]="${VERSION_DATES[$version]}"
    echo "    Pulling $chart:$version"
    helm pull "oci://ghcr.io/${REPO_OWNER,,}/$chart" --version "$version" --destination "$TEMP_DIR" || \
        echo "Failed, skipping"
  done
done

# Count downloaded charts
CHART_COUNT=$(find "$TEMP_DIR" -name "*.tgz" -printf '.' | wc -c)
echo "Downloaded $CHART_COUNT chart packages"

if [ "$CHART_COUNT" -eq 0 ]; then
    echo "No charts downloaded"
    exit 0
fi

# Generate index.yaml
echo "Generating index.yaml..."
helm repo index "$TEMP_DIR" --url "oci://ghcr.io/${REPO_OWNER,,}/${REPO_NAME,,}"

# Update created timestamps with real OCI upload dates
echo "Updating timestamps..."
for key in "${!CHART_DATES[@]}"; do
  # Key format: reponame/chartname:version, need just chartname for index lookup
  chart_path="${key%%:*}"
  chart_name="${chart_path#charts/}"
  chart_version="${key#*:}"
  created_date="${CHART_DATES[$key]}"
  if [ -n "$created_date" ]; then
    yq -i ".entries[\"$chart_name\"] |= (.[] |= select(.version == \"$chart_version\") |= .created = \"$created_date\")" "$TEMP_DIR/index.yaml"
  fi
done

cp "$TEMP_DIR/index.yaml" "$OUTPUT_DIR/"
echo "Index written to $OUTPUT_DIR/index.yaml"
echo "Done!"
