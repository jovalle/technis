#!/bin/sh
set -u

readonly BASE_URL=https://lb.download.kiwix.org/zim
readonly BOOKS_FILE=/config/books.txt
readonly DATA_DIR=/data
readonly LIBRARY_DIR=/library
readonly RELEASE_DIR=$DATA_DIR/releases
readonly STAGING_DIR=$DATA_DIR/staging
readonly LOCK_DIR=/tmp/kiwix-update.lock

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

write_marker() (
  marker=$1
  value=$2
  if ! printf '%s\n' "$value" >"$marker.next"; then
    rm -f "$marker.next"
    return 1
  fi
  mv "$marker.next" "$marker" || {
    rm -f "$marker.next"
    return 1
  }
)

read_marker() (
  marker=$1
  if [ -s "$marker" ]; then
    sed -n '1p' "$marker"
  fi
)

books() (
  sed -e 's/#.*//' -e 's/[[:space:]]//g' "$BOOKS_FILE" | grep -v '^$'
)

rebuild_library() (
  next_library=$LIBRARY_DIR/library.xml.next
  rm -f "$next_library"
  added=0

  for book in $(books); do
    filename=$(read_marker "$LIBRARY_DIR/active-$book")
    if [ -n "$filename" ] && [ -f "$RELEASE_DIR/$filename" ]; then
      kiwix-manage "$next_library" add "$RELEASE_DIR/$filename" >/dev/null || return 1
      added=$((added + 1))
    fi
  done

  [ "$added" -gt 0 ] || return 1
  kiwix-manage "$next_library" show >/dev/null || return 1
  mv "$next_library" "$LIBRARY_DIR/library.xml"
)

wait_for_catalog() (
  filename=$1
  content_name=${filename%.zim}
  attempts=0

  while [ "$attempts" -lt 30 ]; do
    if wget -q -O "$LIBRARY_DIR/catalog.next" \
      'http://kiwix:8080/catalog/v2/entries?count=-1' &&
      grep -Fq "$content_name" "$LIBRARY_DIR/catalog.next"; then
      rm -f "$LIBRARY_DIR/catalog.next"
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 10
  done

  rm -f "$LIBRARY_DIR/catalog.next"
  return 1
)

cleanup_releases() (
  book=$1
  keep=${2:-}
  active=$(read_marker "$LIBRARY_DIR/active-$book")
  previous=$(read_marker "$LIBRARY_DIR/previous-$book")

  for release in "$RELEASE_DIR"/"$book"_????-??.zim; do
    [ -f "$release" ] || continue
    filename=${release##*/}
    if [ "$filename" != "$active" ] && [ "$filename" != "$previous" ] && [ "$filename" != "$keep" ]; then
      rm -f "$release"
    fi
  done
)

update_book() {
  book=$1
  metadata=$LIBRARY_DIR/$book.meta4.next

  log "book=$book state=discover"
  wget -q -O "$metadata" "$BASE_URL/$book.zim.meta4" || return 1

  filename=$(sed -n 's/.*<file name="\([^"]*\)">.*/\1/p' "$metadata" | sed -n '1p')
  expected_size=$(sed -n 's/.*<size>\([0-9][0-9]*\)<\/size>.*/\1/p' "$metadata" | sed -n '1p')
  expected_sha256=$(sed -n 's/.*<hash type="sha-256">\([0-9a-f][0-9a-f]*\)<\/hash>.*/\1/p' "$metadata" | sed -n '1p')

  case "$filename" in
    "$book"_????-??.zim) ;;
    *)
      log "book=$book state=reject reason=filename value=$filename"
      rm -f "$metadata"
      return 1
      ;;
  esac

  # The project directory is not derivable from the book name, so read it back
  # from the metalink mirror list and re-attach it to the trusted download host.
  project=$(sed -n 's|.*<url[^>]*>https://[^<]*/zim/\([^/]*\)/'"$filename"'</url>.*|\1|p' "$metadata" | sed -n '1p')
  rm -f "$metadata"

  case "$project" in
    '' | *[!a-z0-9_-]*)
      log "book=$book state=reject reason=project value=$project"
      return 1
      ;;
  esac
  case "$expected_size" in
    '' | *[!0-9]*)
      log "book=$book state=reject reason=size"
      return 1
      ;;
  esac
  case "$expected_sha256" in
    *[!0-9a-f]* | '')
      log "book=$book state=reject reason=sha256"
      return 1
      ;;
  esac
  [ "${#expected_sha256}" -eq 64 ] || return 1

  active=$(read_marker "$LIBRARY_DIR/active-$book")
  if [ -n "$active" ] && [ ! -f "$RELEASE_DIR/$active" ]; then
    log "book=$book state=reconcile reason=missing-active file=$active"
    rm -f "$LIBRARY_DIR/active-$book" || return 1
    active=
  fi
  previous=$(read_marker "$LIBRARY_DIR/previous-$book")

  for stale in "$STAGING_DIR"/"$book"_????-??.zim; do
    [ -f "$stale" ] || continue
    [ "${stale##*/}" = "$filename" ] || rm -f "$stale" || return 1
  done
  cleanup_releases "$book" "$filename"

  release=$RELEASE_DIR/$filename
  candidate=$STAGING_DIR/$filename
  if [ -f "$release" ]; then
    release_size=$(stat -c %s "$release")
    if [ "$release_size" = "$expected_size" ]; then
      candidate=$release
    elif [ "$active" = "$filename" ]; then
      log "book=$book state=failed reason=active-size actual=$release_size expected=$expected_size"
      return 1
    else
      log "book=$book state=reject reason=release-size actual=$release_size expected=$expected_size"
      rm -f "$release" || return 1
    fi
  fi

  current_size=0
  if [ -f "$candidate" ]; then
    current_size=$(stat -c %s "$candidate")
    if [ "$current_size" -gt "$expected_size" ]; then
      log "book=$book state=reject reason=oversized actual=$current_size expected=$expected_size"
      rm -f "$candidate" || return 1
      current_size=0
    fi
  fi

  available_kib=$(df -Pk "$DATA_DIR" | awk 'NR == 2 {print $4}')
  required_bytes=$((expected_size - current_size + KIWIX_MIN_FREE_BYTES))
  available_bytes=$((available_kib * 1024))
  if [ "$available_bytes" -lt "$required_bytes" ]; then
    log "book=$book state=blocked reason=space available=$available_bytes required=$required_bytes"
    return 1
  fi

  if [ "$candidate" != "$release" ] && [ "$current_size" -lt "$expected_size" ]; then
    log "book=$book state=download file=$filename bytes=$expected_size"
    wget -q -c -O "$candidate" "$BASE_URL/$project/$filename" || return 1
  fi

  actual_size=$(stat -c %s "$candidate")
  if [ "$actual_size" != "$expected_size" ]; then
    log "book=$book state=reject reason=size actual=$actual_size expected=$expected_size"
    rm -f "$candidate" || return 1
    return 1
  fi
  if ! printf '%s  %s\n' "$expected_sha256" "$candidate" | sha256sum -c - >/dev/null; then
    log "book=$book state=reject reason=sha256 file=$filename"
    if [ "$candidate" != "$release" ] || [ "$active" != "$filename" ]; then
      rm -f "$candidate" || return 1
    fi
    return 1
  fi

  validation_library=$LIBRARY_DIR/validation.xml
  rm -f "$validation_library"
  kiwix-manage "$validation_library" add "$candidate" >/dev/null || return 1
  rm -f "$validation_library"

  if [ "$active" = "$filename" ] && [ "$candidate" = "$release" ]; then
    cleanup_releases "$book"
    log "book=$book state=current file=$filename"
    return 0
  fi

  if [ "$candidate" != "$release" ]; then
    mv "$candidate" "$release" || exit 1
  fi
  if [ -n "$active" ]; then
    write_marker "$LIBRARY_DIR/previous-$book" "$active" || exit 1
  fi
  if [ -f "$LIBRARY_DIR/library.xml" ]; then
    cp "$LIBRARY_DIR/library.xml" "$LIBRARY_DIR/library.xml.rollback" || exit 1
  fi
  write_marker "$LIBRARY_DIR/active-$book" "$filename" || exit 1

  if ! rebuild_library || ! wait_for_catalog "$filename"; then
    log "book=$book state=rollback file=$filename"
    if [ -n "$active" ]; then
      write_marker "$LIBRARY_DIR/active-$book" "$active" || exit 1
    else
      rm -f "$LIBRARY_DIR/active-$book" || exit 1
    fi
    if [ -f "$LIBRARY_DIR/library.xml.rollback" ]; then
      mv "$LIBRARY_DIR/library.xml.rollback" "$LIBRARY_DIR/library.xml" || exit 1
    else
      rm -f "$LIBRARY_DIR/library.xml" || exit 1
    fi
    if [ -n "$previous" ]; then
      write_marker "$LIBRARY_DIR/previous-$book" "$previous" || exit 1
    else
      rm -f "$LIBRARY_DIR/previous-$book" || exit 1
    fi
    return 1
  fi

  rm -f "$LIBRARY_DIR/library.xml.rollback"
  cleanup_releases "$book"
  log "book=$book state=active file=$filename"
}

run_update() {
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "state=skip reason=locked"
    return 1
  fi
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  result=0
  for book in $(books); do
    update_book "$book" || result=1
  done
  if [ "$result" -eq 0 ]; then
    write_marker "$LIBRARY_DIR/last-success" "$(date -u +%s)" || result=1
  fi

  rmdir "$LOCK_DIR"
  trap - EXIT INT TERM
  return "$result"
}

is_listed() (
  for candidate in $(books); do
    [ "$candidate" = "$1" ] && return 0
  done
  return 1
)

prune_unlisted() {
  for marker in "$LIBRARY_DIR"/active-* "$LIBRARY_DIR"/previous-*; do
    [ -f "$marker" ] || continue
    name=${marker##*/}
    book=${name#active-}
    book=${book#previous-}
    if ! is_listed "$book"; then
      log "book=$book state=retire reason=unlisted"
      rm -f "$marker" || exit 1
    fi
  done

  for release in "$RELEASE_DIR"/*.zim "$STAGING_DIR"/*.zim; do
    [ -f "$release" ] || continue
    filename=${release##*/}
    book=${filename%_????-??.zim}
    if [ "$book" != "$filename" ] && ! is_listed "$book"; then
      rm -f "$release" || exit 1
    fi
  done
}

mkdir -p "$RELEASE_DIR" "$STAGING_DIR" "$LIBRARY_DIR"

book_count=0
for book in $(books); do
  case "$book" in
    [A-Za-z0-9]*) ;;
    *)
      log "state=failed reason=book-name value=$book"
      exit 1
      ;;
  esac
  case "$book" in
    *[!A-Za-z0-9._-]*)
      log "state=failed reason=book-name value=$book"
      exit 1
      ;;
  esac
  book_count=$((book_count + 1))
done

# An empty list would make prune_unlisted retire every book, so refuse to run on
# a missing or unreadable mount rather than deleting the served content.
if [ "$book_count" -eq 0 ]; then
  log "state=failed reason=books-empty path=$BOOKS_FILE"
  exit 1
fi

# Migrate the pre-book-list state, which keyed Wikipedia markers by language.
for language in en es; do
  for kind in active previous; do
    legacy=$LIBRARY_DIR/$kind-$language
    [ -f "$legacy" ] || continue
    target=$LIBRARY_DIR/$kind-wikipedia_${language}_all_maxi
    if [ -e "$target" ]; then
      rm -f "$legacy" || exit 1
    else
      mv "$legacy" "$target" || exit 1
    fi
  done
done

prune_unlisted

valid_active=0
for book in $(books); do
  filename=$(read_marker "$LIBRARY_DIR/active-$book")
  if [ -n "$filename" ] && [ -f "$RELEASE_DIR/$filename" ]; then
    valid_active=$((valid_active + 1))
  elif [ -n "$filename" ]; then
    log "book=$book state=reconcile reason=missing-active file=$filename"
    rm -f "$LIBRARY_DIR/active-$book" || exit 1
  fi
done
if [ "$valid_active" -gt 0 ]; then
  rebuild_library || {
    log "state=failed reason=library-reconcile"
    exit 1
  }
else
  rm -f "$LIBRARY_DIR/library.xml" "$LIBRARY_DIR/library.xml.next"
fi
while true; do
  if run_update; then
    sleep "$KIWIX_UPDATE_INTERVAL"
  else
    sleep "$KIWIX_RETRY_INTERVAL"
  fi
done
