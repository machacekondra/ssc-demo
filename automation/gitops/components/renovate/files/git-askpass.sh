#!/bin/sh
case "$1" in
  *sername*) printf '%s' "${RENOVATE_USERNAME:-oauth2}" ;;
  *assword*) printf '%s' "$RENOVATE_TOKEN" ;;
  *) exit 1 ;;
esac
