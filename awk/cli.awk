#!/usr/bin/gawk -bE

# Tool/utilities for bup

# Consider rewriting in Nim and using database: https://capocasa.github.io/limdb/limdb.html

# The MIT License (MIT)
#
# Copyright (c) 2022 by User:GreenC (at en.wikipedia.org)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

@include "library"
@include "json"

BEGIN {

  G["Home"] = "/data/project/bup/"
  G["WWW"] = G["Home"] "www/python/src/templates/"
  G["static"] = G["Home"] "www/python/src/static/"
  G["db"] = "/data/project/bup/www/db/"
  G["out.json"] = G["db"] "out.json"
  G["tablelength"] = 75 # number of articles in table

  G["showexpired"] = 0 # 1 = in preview mode show old cites that it was unable to find

  Exe["grep"] = "/bin/grep"
  Exe["mv"] = "/bin/mv"
  Exe["cp"] = "/bin/cp"
  Exe["sed"] = "/bin/sed"
  Exe["jq"] = "/usr/bin/jq"
  Exe["sponge"] = "/usr/bin/sponge"
  Exe["wikiget"] = "/data/project/bup/BotWikiAwk/bin/wikiget"

  tableid = 0
  Optind = Opterr = 1
  while ((C = getopt(ARGC, ARGV, "m:p:i:n:c:r:d:u:o:e:")) != -1) {
      opts++
      if(C == "m")  {              #  -m "table"     make main page. "table" means create HTML table. "tablefp" returns path to JSON file.
        type = "makemain"
        makeopt = strip(Optarg)
      }
      if(C == "p") {               #  -p <tablefp>   make preview/analysis page for a given -i <#>
        type = "makepreview"
        tablefp = strip(Optarg)
      }
      if(C == "n") {               #  -n <tablefp>   get page name for a given -i <#>
        type = "getpagename"
        tablefp = strip(Optarg)
      }
      if(C == "c") {               #  -c <tablefp>   get edit count for a given -i <#>
        type = "getpagecount"
        tablefp = strip(Optarg)
      }
      if(C == "e") {               #  -e <pagename>  get edit count for on-demand 
        type = "getpagecountondemand"
        pagename = strip(Optarg)
      }
      if(C == "r") {               #  -r <tablefp>   make new page HTML for a given -i <#>
        type = "makepagehtml"
        tablefp = strip(Optarg)
      }
      if(C == "o") {               #  -o <pagename>  run on-demand on behalf a -u <username>
        type = "makepageondemand"
        pagename = strip(Optarg)
      }
      if(C == "d") {               #  -d <tablefp>   make done page. Use with -i <#> == pagecount
        type = "makepagedone"
        pagename = strip(Optarg)
      }
      if(C == "i")                 #  -i <id>        line number in tablefp
        tableid = Optarg
      if(C == "u")
        username = Optarg          #  -u <username>  Username running bot
  }

  if(type == "makemain") {
    fpTable = mktemp(G["db"] "table.XXXXXX", "u")
    loadT(fpTable)
    if(makeopt == "table")
      print makemain() 
    else if(makeopt == "tablefp")
      print fpTable
  }
  else if(type == "makepreview") {
    if(empty(tablefp) || tableid == 0) 
      exit 1
    print makepreview()
  }
  else if(type == "getpagename") {
    if(empty(tablefp) || tableid == 0) 
      exit 1
    print getpagename()
  }
  else if(type == "getpagecount") {
    if(empty(tablefp) || tableid == 0) 
      exit 1
    print getpagecount()
  }
  else if(type == "getpagecountondemand") {
    if(empty(pagename)) 
      exit 1
    print getpagecountondemand(pagename)
  }
  else if(type == "makepagehtml") {
    if(empty(tablefp) || tableid == 0) 
      exit 1
    print makepagehtml()
  }
  else if(type == "makepageondemand") {
    if(empty(pagename))
      exit 1
    print makepageondemand(pagename)
  }
  else if(type == "makepagedone") {
    if(empty(pagename)) 
      exit 1
    print makepagedone(pagename, tableid)
  }

  exit 0

}

#
# Get page name 
#
function getpagename(  command,jsonin,jsona) {

  command = Exe["sed"] " -n \"" tableid " {;p;q;}\" " tablefp
  jsonin = sys2var(command)
  if( query_json(jsonin, jsona) >= 0) 
    return strip(jsona["page"])

}

#
# Get page count
#
function getpagecount(  command,jsonin,jsona,numofcites,wpfp,i,oldcite,count,wppage) {

  count = 0
  command = Exe["sed"] " -n \"" tableid " {;p;q;}\" " tablefp
  jsonin = sys2var(command)
  if( query_json(jsonin, jsona) >= 0) {
    numofcites = jsona["citations","0"]
    wppage = strip(jsona["page"])
    wpfp = sys2var(Exe["wikiget"] " -w " shquote(wppage))
    for(i = 1; i <= numofcites; i++) {
      oldcite = jsona["citations",i,"oldcite"]
      if(countsubstring(wpfp, oldcite) > 0) 
        count++
    }
  }
  return strip(count)

}

#
# Get page count on-demand
#
function getpagecountondemand(pagename,  command,jsonin,jsona,numofcites,wpfp,i,oldcite,count,wppage) {

  count = 0
  command = Exe["grep"] " -F -m1 " shquote("\"page\":\"" pagename "\"") " " G["db"] "out.json.all"
  jsonin = sys2var(command)
  if( query_json(jsonin, jsona) >= 0) {
    numofcites = jsona["citations","0"]
    wppage = strip(jsona["page"])
    wpfp = sys2var(Exe["wikiget"] " -w " shquote(wppage))
    for(i = 1; i <= numofcites; i++) {
      oldcite = jsona["citations",i,"oldcite"]
      if(countsubstring(wpfp, oldcite) > 0) 
        count++
    }
  }
  return strip(count)

}

function doquote(s) {
  return gsubs("\"", "\\\"",s)
}

#
# Make page HTML
#
function makepagehtml(  command,jsonin,jsona,numofcites,wpfp,i,oldcite,newcite,wppage) {

  command = Exe["sed"] " -n \"" tableid " {;p;q;}\" " tablefp
  jsonin = sys2var(command)
  if( query_json(jsonin, jsona) >= 0) {
    numofcites = jsona["citations","0"]
    wppage = strip(jsona["page"])
    wpfp = sys2var(Exe["wikiget"] " -w " shquote(wppage))
    for(i = 1; i <= numofcites; i++) {
      oldcite = jsona["citations",i,"oldcite"]
      newcite = jsona["citations",i,"newcite"]
      if(countsubstring(wpfp, oldcite) > 0) 
        wpfp = gsubs(oldcite, newcite, wpfp)
    }

    # Mark done in the database    
    #command = Exe["jq"] " -s -c '.[] | select (.page == \"" doquote(wppage) "\") .done = \"1\"' " G["db"] "out.json | " Exe["sponge"] " " G["db"] "out.json"
    #print command >> G["db"] "jqlog.txt"
    # sys2var(command)
    setjson(wppage)
  }
  return wpfp

}

#
# Make page on-demand
#
function makepageondemand(pagename,  command,jsonin,jsona,numofcites,wpfp,i,oldcite,newcite,count,wppage) {

  command = Exe["grep"] " -F -m1 " shquote("\"page\":\"" pagename "\"") " " G["db"] "out.json.all"
  jsonin = sys2var(command)
  if( query_json(jsonin, jsona) >= 0) {
    numofcites = jsona["citations","0"]
    wppage = strip(jsona["page"])
    wpfp = sys2var(Exe["wikiget"] " -w " shquote(wppage))
    for(i = 1; i <= numofcites; i++) {
      oldcite = jsona["citations",i,"oldcite"]
      newcite = jsona["citations",i,"newcite"]
      if(countsubstring(wpfp, oldcite) > 0) 
        wpfp = gsubs(oldcite, newcite, wpfp)
    }
    setjson(wppage)    
  }
  return wpfp
}

#
# Make page done
#
function makepagedone(wppage,pagecount,  out) {

  out = readfile("/data/project/bup/www/awk/tableheader.html")

  if(pagecount > 0) {
    out = out "\n" "<p>Success! Added " pagecount " book link(s) to: <a href=\"https://en.wikipedia.org/wiki/" wppage "\">" wppage "</a> (<a href=\"https://en.wikipedia.org/w/index.php?title=" wppage "&action=history\">view edit history</a>)<br><br></p>"
    out = out "\n" "<p><a href=\" {{url_for('main')}} \">Return to list</a>"
  }
  else {
    out = out "\n" "<br>No active cites found (2). Article <" wppage "> removed from list.<br><br>"
    out = out "\n" "<a href=\" {{url_for('main')}} \">Return to list.</a>"
    command = Exe["jq"] " -s -c '.[] | select (.page == \"" doquote(wppage) "\") .done = \"1\"' " G["db"] "out.json | " Exe["sponge"] " " G["db"] "out.json"
    print wppage " ---- " numofcites " ---- No active cites found (2) ---- " command >> G["db"] "errorlog.txt"
    close(G["db"] "errorlog.txt")
    setjson(wppage)
  }

  out = out "\n" "</body>"
  out = out "\n" "</html>"
  return out

}

#
# make preview.html
#
function makepreview(  out,outT,expired,command,jsonin,jsona,numofcites,wppage,oldcite,newcite,i,d,iaurl) {

  out = readfile("/data/project/bup/www/awk/tableheader.html")
  out = out "\n" "<center>"
  # out = out "\n" "<form method=\"post\">" 
  out = out "\n" "<table class=\"sortable\">" 
  out = out "\n" "<thead>" 
  out = out "\n" "  <tr>" 

  out = out "\n" "    <th><u>Original cite</u></th>" 
  out = out "\n" "    <th><u>Proposed cite</u></th>" 

  out = out "\n" "  </tr>" 
  out = out "\n" "</thead>" 
  out = out "\n" "<style type=\"text/css\">" 
  out = out "\n" "table.sortable tbody {" 
  out = out "\n" "  text-align: left;"
  out = out "\n" "}" 
  out = out "\n" "table.sortable tfoot {"
  out = out "\n" "  text-align: right;" 
  out = out "\n" "}"
  out = out "\n" "</style>" 

  # Fastest to print a line by number: sed -n "1000 {;p;q;}" file.txt
  command = Exe["sed"] " -n \"" tableid " {;p;q;}\" " tablefp
  # out = out "\n" command 
  jsonin = sys2var(command)

  outT = "<tbody>" 

  if( query_json(jsonin, jsona) >= 0) {
    numofcites = jsona["citations","0"]
    wppage = jsona["page"]
    wpfp = sys2var(Exe["wikiget"] " -w " shquote(wppage))
    for(i = 1; i <= numofcites; i++) {
      oldcite = jsona["citations",i,"oldcite"]
      newcite = jsona["citations",i,"newcite"]
      if(match(newcite, /https[:]\/\/archive[.]org\/details\/[^\/]*\/page\/[^ ]*[^ ]/, d) > 0) 
        iaurl = d[0]
      else
        iaurl = "https://archive.org/details/" jsona["citations",i,"iaid"]

      if(G["showexpired"]) {
        if(countsubstring(wpfp, oldcite) == 0) {
          newcite = "<mark class=\"red\">Old cite no longer visible in article. Deleted? Modified?</mark>"
          expired++
        }
      }
      else {
        if(countsubstring(wpfp, oldcite) == 0) {
          expired++
          continue
        }
      }

      outT = outT "\n" "  <tr>" 
      #<input type="checkbox" id="id_{{i.username}}" name="users" value="{{i.id}}">
      #<label for="id_{{i.username}}">{{i.username}}</label>
      # out = out "\n" "    <td><input type=\"checkbox\" name=\"preview_checkbox\" value=\"" i "\" checked></td>" 
      outT = outT "\n" "    <td>{% raw %}" oldcite "{% endraw %}</td>" 
      outT = outT "\n" "    <td>{% raw %}" colorcite(newcite, iaurl) "{% endraw %}</td>" 
      outT = outT "\n" "  <tr>"
    }

  }
  outT = outT "\n" "</tbody>" 
  outT = outT "\n" "<tfoot>"
  outT = outT "\n" "</table>" 
  outT = outT "\n" "</center>"
  # outT = outT "\n" "</form>" 

  out = out "\n" "<center><h2>BUP - Books Up!</h2></center>"
  out = out "\n" "<center><h3>Analysis: <a href=\"https://en.wikipedia.org/wiki/" wppage "\">" wppage "</a></h3></center>"
  if(G["showexpired"])
    out = out "\n" "<center>Original cites: " numofcites ". Proposed cites available: " numofcites - expired "</center>"
  else
    out = out "\n" "<center>Proposed cites available: " numofcites - expired "</center>"
  out = out "\n" "<center><a href=\" {{url_for('main')}} \">Return to article list</a>"

  out = out "\n" outT
  # out = out "\n" "<a href=\" {{url_for('submit2')}} \"><input type=\"submit\" value=\"Run bot\"></a>" 

  if(empty(wppage) || numofcites == 0) {
    out = readfile("/data/project/bup/www/awk/tableheader.html")
    out = out "\n" "<br>Error finding record in database (1).<br><br>"
    out = out "\n" "<a href=\" {{url_for('main')}} \">Return to list.</a>"
    print tablefp " ---- " tableid " ---- Error finding record in database (1)" >> G["db"] "errorlog.txt" 
  }

  if(numofcites - expired == 0) {
    out = readfile("/data/project/bup/www/awk/tableheader.html")
    out = out "\n" "<br>No active cites found (1). Article <" wppage "> removed from list.<br><br>"
    out = out "\n" "<a href=\" {{url_for('main')}} \">Return to list.</a>"
    command = Exe["jq"] " -s -c '.[] | select (.page == \"" doquote(wppage) "\") .done = \"1\"' " G["db"] "out.json | " Exe["sponge"] " " G["db"] "out.json"
    # command = Exe["jq"] " -s -c '.[] | select (.page == \"" doquote(wppage) "\") .done = \"1\"' " G["db"] "out.json > " G["db"] "o"
    print wppage " ---- " numofcites " ---- No active cites found (1) ---- " command >> G["db"] "errorlog.txt"
    # sys2var(command) 
    setjson(wppage)
  }

  out = out "\n" "</body>" 
  out = out "\n" "</html>"

  return out

}

#
# make main.html
#
function makemain(  out,outT,i,id) {

  out = readfile("/data/project/bup/www/awk/tableheader.html")
  out = out "\n" "<center>" 
  out = out "\n" "<table class=\"sortable\">"
  out = out "\n" "<thead>"
  out = out "\n" "  <tr>"

  out = out "\n" "    <th><u>Preview</u></th>"
  out = out "\n" "    <th><u>Run bot</u></th>"
  out = out "\n" "    <th><u>Article</u></th>"
  out = out "\n" "    <th><u>Regular books</u></th>"
  out = out "\n" "    <th><u>SIM books</u></th>"
  out = out "\n" "    <th><u>Refactor existing books</u></th>"
  out = out "\n" "    <th><u>Total avail. books</u></th>"

  out = out "\n" "  </tr>"
  out = out "\n" "</thead>"
  out = out "\n" "<style type=\"text/css\">"
  out = out "\n" "table.sortable tbody {"
  out = out "\n" "  text-align: right;"
  out = out "\n" "}"
  out = out "\n" "table.sortable tfoot {"
  out = out "\n" "  text-align: right;"
  out = out "\n" "}"
  out = out "\n" "</style>"

  outT = outT "\n" "<tbody>"

  id = 0
  for(i = 1; i <= length(TK); i++) {
    id = id + 1
    outT = outT "\n" "  <tr>"
    # https://stackoverflow.com/questions/7478366/create-dynamic-urls-in-flask-with-url-for
    outT = outT "\n" "    <td><a href=\"{{url_for('preview', id = " id ")}}\" target=\"_blank\" rel=\"noopener noreferrer\"><input type=\"submit\" value=\"Preview\"></a></td>"
    outT = outT "\n" "    <td><a href=\"{{url_for('runbot', id = " id ")}}\" target=\"_blank\" rel=\"noopener noreferrer\"><input type=\"submit\" value=\"Run bot\"></a></td>"
    outT = outT "\n" "    <td><a href=\"https://en.wikipedia.org/wiki/" TK[i] "\" target=\"_blank\" rel=\"noopener noreferrer\">" TK[i] "</a></td>"
    outT = outT "\n" "    <td>" T[TK[i]]["book_count"] "</td>"
    outT = outT "\n" "    <td>" T[TK[i]]["sim_count"] "</td>"
    outT = outT "\n" "    <td>" T[TK[i]]["ref_count"] "</td>"
    outT = outT "\n" "    <td>" T[TK[i]]["count"] "</td>"
    outT = outT "\n" "  </tr>"
  }
  outT = outT "\n" "</tbody>"
  outT = outT "\n" "<tfoot>"
  outT = outT "\n" "</table>"
  outT = outT "\n" "</center>"

  out = out "\n" "<font size=\"+2\">BUP - Books Up!</font><br><br>"
  out = out "\n" "<a href=\"{{url_for('about')}}\">About</a> | <a href=\"{{url_for('logout')}}\">Logout</a> ({{ username }})<br><br>"
  out = out "\n" "<input type=\"submit\" value=\"Preview\"> = show proposed edit. <input type=\"submit\" value=\"Run bot\"> = make the edit. Optional: "

  # On-demand input box
  if(username == "GreenC" || username == "Markjgraham hmb" || username == "Brewsterkahle") {
    out = out "\n" "<form action=\"{{ url_for('ondemand') }}\" method=\"POST\">"
    out = out "\n" "  <input name=\"text\" placeholder=\"<article name>\">"
    out = out "\n" "  <input type=\"submit\" value=\"Run bot on <article name>\">"
    out = out "\n" "</form>"
  }

  out = out "\n" outT

  out = out "\n" "</center>"
  out = out "\n" "</body>"
  out = out "\n" "</html>" 

  return out

}

#
# Load first x number of JSON records from out.json (with done:0) into T[][]
#   grep is 20x faster than jq
#
function loadT(fpTable,  i,a,d,page,c,b,re,out) {

  if(checkexists(fpTable))
    removefile2(fpTable)

  for(i = 1; i <= splitn(sys2var(Exe["grep"] " -m " G["tablelength"] " -F '\"done\":\"0\"' " G["out.json"]) "\n", a, i); i++) {

    print a[i] >> fpTable

    # "count":114,"ref_count":0,"sim_count":0,"book_count":114
    match(a[i], /"page"[:]"[^"]*"/,d)
    gsub(/(^"page"[:]"|"$)/, "", d[0])
    page = d[0]
    TK[i] = page  # tracking array since associatives are un-ordered and we need to maintain order of addition
    T[page]["page"] = page
    c = split("count ref_count sim_count book_count", b, " ")
    for(ii = 1; ii <= c; ii++) {
      re = "\"" b[ii] "\"[:][0-9]{1,3}"
      match(a[i], re, d)
      re = "\"" b[ii] "\"[:]"
      gsub(re, "", d[0])
      T[page][b[ii]] = d[0]
    }
  }

}

function colorcite(cite, iaurl, plainiaurl) {

  if(iaurl ~ /\/page\//) {
    cite = gsubs(iaurl, "__HIDE__", cite)
    plainiaurl = iaurl
    sub(/\/page\/[^$]*$/, "", plainiaurl)
    cite = gsubs(plainiaurl, "<mark class=\"red\"><a href=\"" plainiaurl "\">" plainiaurl "</a></mark>", cite)
  }
  else
    cite = gsubs(iaurl, "__HIDE__", cite)

  if(iaurl ~ /\/page\//) 
    cite = gsubs("__HIDE__", "<mark class=\"red\"><a href=\"" iaurl "\">" iaurl "</a></mark>", cite)
  else
    cite = gsubs("__HIDE__", "<mark class=\"red\"><a href=\"" iaurl "\">" iaurl "</a></mark>", cite)

  return cite

}

#
# Set status in out.json with some primitive error-prone file locking
#
function setjson(wppage,  free,i,command,jsub) {

  if(checklock() == 0) {
    # jsub = "/usr/bin/jsub -sync y -once -quiet -N runjq.awk -l mem_free=200M,h_vmem=400M -e " G["Home"] "runjq.stderr -o " G["Home"] "runjq.stdout -v \"AWKPATH=.:/data/project/bup/BotWikiAwk/lib\" -v \"PATH=/sbin:/bin:/usr/sbin:/usr/local/bin:/usr/bin:/data/project/bup/BotWikiAwk/bin\" -wd " G["Home"] "www/awk " G["Home"] "www/awk/runjq.awk " shquote(wppage)
    command = Exe["jq"] " -s -c '.[] | select (.page == \"" doquote(wppage) "\") .done = \"1\"' " G["db"] "out.json > " G["db"] "json.temp"
    # print command >> G["db"] "debug.txt"
    sys2var(command)
    close(G["db"] "json.temp")
    command = Exe["mv"] " " G["db"] "json.temp " G["db"] "out.json"
    sys2var(command)
    close(G["db"] "out.json")

    # Log clearit
    print "/usr/bin/jq -s -c '.[] | select (.page == \"" wppage "\") .done = \"1\"' /data/project/bup/www/db/out.json  > /data/project/bup/www/db/o; /usr/bin/mv /data/project/bup/www/db/o /data/project/bup/www/db/out.json" >> G["db"] "clearit"
    close(G["db"] "clearit")

    return 1


  }
  else
    return 0

}


#
# Return "1" if json.temp exists
# Return "0" if json.temp does not exist
#
function checklock( i,free) {

  free = 1
  system("")
  for(i = 1; i <= 10; i++) {
    if(checkexists(G["db"] "json.temp")) {
      sleep(5, "unix") 
    }
    else {
      free = 0
      break
    }
    system("")
  }
  return free

}
