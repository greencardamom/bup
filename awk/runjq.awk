#!/usr/bin/gawk -bE

@include "/data/project/bup/BotWikiAwk/lib/library"

BEGIN {
  n = ARGV[1]
  sys2var("/usr/bin/jq -s -c '.[] | select (.page == \"" doquote(n) "\") .done = \"1\"' /data/project/bup/www/db/out.json | /usr/bin/sponge /data/project/bup/www/db/out.json")
}

function doquote(s) {
  return gsubs("\"", "\\\"",s)
}

