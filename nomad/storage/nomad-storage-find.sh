#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
#
# nomad-storage-find: the one command `nomad-storage.sh -O -F THIS` runs as
# each folder's owner, through sudo. Where sudo would otherwise have to let
# the account run find itself as the others (find can run any program, and
# delete or write files), sudoers allows only this:
#
#     ACCOUNT ALL=(ALL,!root) NOPASSWD: /usr/local/sbin/nomad-storage-find
#
# It takes absolute folder paths and nothing else, and runs find over them
# with a fixed expression that only prints: each folder's depth, owner and
# path (and an X after one the account can't list or enter), each file's
# size in 512-byte blocks, time, links, inode and path.
# It reads names and sizes, never a file's contents, and changes nothing.
# Install it owned by root, mode 755, in a folder only root can change:
#
#     nomad storage script --find > nomad-storage-find
#     install -o root -g root -m 755 nomad-storage-find /usr/local/sbin/nomad-storage-find
#
# Keep it the same as nomad-storage.sh's own (nomad storage script --find
# prints the one that matches the installed nomad).
# nomad-storage.sh -F checks for this line: the wrapper, and its version.
# shellcheck disable=SC2034
NOMAD_STORAGE_FIND=1
PATH=/usr/bin:/bin
export PATH
NL='
'
[ $# -gt 0 ] || { echo "nomad-storage-find: give absolute folder paths" >&2; exit 2; }
for a in "$@"; do
    case $a in
        *"$NL"*) echo "nomad-storage-find: a path with a line break" >&2; exit 2 ;;
        /*) ;;
        *) echo "nomad-storage-find: absolute folder paths only" >&2; exit 2 ;;
    esac
done
# From /, which every account may enter. Every argument starts with /, so
# find takes each one as a starting point, never as part of the expression.
cd / || exit 2
exec find "$@" -xdev \
    \( -type d \( -path "*$NL*" -o -printf 'D %d %U %p\n' \) \( -readable -executable -o -printf 'X\n' \) \) -o \
    \( -type f \( -path "*$NL*" -printf 'N %b %T@ %n %i %H\n' -o -printf 'L %b %T@ %n %i %p\n' \) \)
