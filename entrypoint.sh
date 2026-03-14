#!/bin/sh
set -eu

for path in /captures /logs; do
    if [ -d "$path" ]; then
        echo "Fixing ownership under $path for ankerctl..."
        chown -R ankerctl:ankerctl "$path"
    fi
done

exec gosu ankerctl "$@"
