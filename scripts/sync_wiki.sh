#!/usr/bin/env bash
# ==============================================================================
# CypherSOL - GitHub Wiki Synchronization Utility
# Syncs wiki/ Markdown files to the GitHub repository wiki:
#   https://github.com/GodIsAMyth/cyphersol.wiki.git
# ==============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WIKI_DIR="${REPO_ROOT}/wiki"
WIKI_REPO_URL="https://github.com/GodIsAMyth/cyphersol.wiki.git"
TEMP_DIR="$(mktemp -d -t cyphersol_wiki_XXXXXX)"

cleanup() {
    rm -rf "${TEMP_DIR}"
}
trap cleanup EXIT

echo "=== Syncing CypherSOL Guides to GitHub Wiki ==="
echo "Wiki directory: ${WIKI_DIR}"

# 1. Update wiki files from docs/
cp "${REPO_ROOT}/docs/USER_GUIDE.md" "${WIKI_DIR}/User-Guide.md"
cp "${REPO_ROOT}/docs/DEPLOYMENT_GUIDE.md" "${WIKI_DIR}/Deployment-Guide.md"
cp "${REPO_ROOT}/docs/DEVELOPER_AND_ARCHITECTURE_GUIDE.md" "${WIKI_DIR}/Developer-and-Architecture-Guide.md"
cp "${REPO_ROOT}/docs/DATA_AND_VARIABLE_DICTIONARY.md" "${WIKI_DIR}/Data-and-Variable-Dictionary.md"

# 2. Clone or initialize remote wiki repo
echo "Connecting to ${WIKI_REPO_URL}..."
if git clone "${WIKI_REPO_URL}" "${TEMP_DIR}/wiki_clone" 2>/dev/null; then
    cd "${TEMP_DIR}/wiki_clone"
    cp -r "${WIKI_DIR}/"* .
    git add -A
    if git diff --staged --quiet; then
        echo "Wiki is already up to date!"
    else
        git commit -m "Update CypherSOL Wiki guides (User Guide, Deployment, Architecture)"
        git push origin master || git push origin main
        echo "Wiki successfully pushed to GitHub!"
    fi
else
    echo "Notice: GitHub requires the first wiki page to be created in the GitHub UI before the wiki git repository is initialized."
    echo "To initialize the wiki:"
    echo "  1. Navigate to: https://github.com/GodIsAMyth/cyphersol/wiki in your browser"
    echo "  2. Click 'Create the first page' and save it."
    echo "  3. Re-run: ./scripts/sync_wiki.sh"
    echo ""
    echo "All wiki files are maintained in '${WIKI_DIR}/' in the main repository."
fi
