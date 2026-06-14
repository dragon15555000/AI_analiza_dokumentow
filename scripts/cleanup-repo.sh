#!/bin/bash
set -euo pipefail

# cleanup-repo.sh
# Pełny skrypt czyszczący repozytorium Git
# - Przed/po raport
# - Prune remote branches
# - Usuwanie zmergowanych lokalnych gałęzi (oprócz master/main)
# - git gc --prune=now --aggressive
# - Opcjonalnie: usuwanie unreachable obiektów

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== REPO CLEANUP SCRIPT ==="
echo "Working in: $REPO_ROOT"
echo "Date: $(date)"
echo ""

# Function to print stats
print_stats() {
    echo "=== STATS ==="
    echo "git count-objects -vH:"
    git count-objects -vH
    echo ""
    echo "Unreachable objects:"
    git fsck --unreachable 2>/dev/null | wc -l
    echo ""
    echo ".git size:"
    du -sh .git
    echo ""
    echo "Refs count:"
    git for-each-ref | wc -l
    echo ""
}

echo "=== BEFORE CLEANUP ==="
print_stats

echo "=== STEP 1: Fetch + prune remotes ==="
git fetch --all --prune
git remote prune origin || true
echo ""

echo "=== STEP 2: Delete merged local branches (except master/main) ==="
current_branch=$(git branch --show-current)
for branch in $(git branch --merged | grep -vE '^\*|^  (master|main)$' | sed 's/^  //'); do
    if [ "$branch" != "$current_branch" ]; then
        echo "Deleting merged branch: $branch"
        git branch -d "$branch" || echo "  (skipped, not fully merged or protected)"
    fi
done
echo ""

echo "=== STEP 3: Aggressive GC ==="
echo "Running git gc --prune=now --aggressive (this may take a while)..."
git gc --prune=now --aggressive
echo ""

echo "=== AFTER CLEANUP ==="
print_stats

echo "=== FINAL REPORT ==="
echo "Unreachable objects after gc:"
git fsck --unreachable 2>/dev/null | wc -l
echo ""
echo "If you want to force-delete any remaining unreachable objects (destructive):"
echo "  git fsck --unreachable | awk '{print \$3}' | xargs git update-ref -d 2>/dev/null || true"
echo "  git gc --prune=now --aggressive"
echo ""
echo "Done. Repo should be leaner now."
