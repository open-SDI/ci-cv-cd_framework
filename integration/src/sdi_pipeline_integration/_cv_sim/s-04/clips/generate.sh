#!/bin/sh
# Reproduces the committed clips byte for byte; sox -D skips its random dither.
set -eu
cd "$(dirname "$0")"
docker run --rm --platform linux/amd64 -v "$PWD:/clips" -e OWNER="$(id -u):$(id -g)" \
    ubuntu:noble@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 \
    sh -euc '
rm -f /etc/apt/sources.list.d/ubuntu.sources
printf "%s\n" "Types: deb" "URIs: https://snapshot.ubuntu.com/ubuntu/20261012T164422Z/" \
    "Suites: noble noble-updates noble-security" "Components: main universe" \
    "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg" \
    > /etc/apt/sources.list.d/ubuntu-snapshot.sources
apt-get -qq -o Acquire::https::Verify-Peer=false update
apt-get -qq -o Acquire::https::Verify-Peer=false install -y --no-install-recommends \
    espeak-ng=1.51+dfsg-12build1 sox=14.4.2+git20190427-4build4 >/dev/null
speak() {
    espeak-ng -v en-us -w /tmp/speech.wav "$2"
    sox -D /tmp/speech.wav -r 16000 -c 1 -b 16 "/clips/$1"
    chown "$OWNER" "/clips/$1"
}
speak request.wav "Please take this book to Joe."
speak receipt.wav "I got the book, thank you."
'
