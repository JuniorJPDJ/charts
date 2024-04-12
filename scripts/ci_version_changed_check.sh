#!/bin/bash
GIT_DIFF_BASE="$1"
GIT_DIFF_HEAD="$2"
EXIT_CODE=0
CHANGED=0

[ -z "$GIT_DIFF_BASE" -o -z "$GIT_DIFF_HEAD" ] &&
  echo "GIT_DIFF_BASE or GIT_DIFF_HEAD not provided. Exiting." &&
  exit 1

cd charts

for d in * ; do
  # check if directory changed and skip if not
  git diff -s --exit-code "${GIT_DIFF_BASE}..${GIT_DIFF_HEAD}" -- "$d" && \
    continue
  CHANGED=1
  echo "[Chart $d]: files changed, checking versions"

  chart_yaml="./$d/Chart.yaml"
  if [ -f "$chart_yaml" ] ; then
    base_ver="$(git show "$GIT_DIFF_BASE:$chart_yaml" | grep "^version:" | awk '{print $2}')"
    echo "[Chart $d]: base version: $base_ver"
    head_ver="$(git show "$GIT_DIFF_HEAD:$chart_yaml" | grep "^version:" | awk '{print $2}')"
    echo "[Chart $d]: head version: $head_ver"

    if [ "$base_ver" == "$head_ver" ] ; then
      echo -e "[Chart $d]: \e[31mversion not changed, this should be fixed!\e[0m"
      EXIT_CODE=2
    else
      echo -e "[Chart $d]: \e[32mversion changed, everything is ok!\e[0m"
    fi
  fi
done

[ "$CHANGED" == "0" ] && \
  echo -e "\e[32mNo chart changes detected, everything is ok!\e[0m"
exit $EXIT_CODE
