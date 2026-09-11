# Record the README demo:
#   brew install vhs
#   set -a && . ./.env && set +a          # exports WS_API_KEY
#   vhs scripts/demo.tape               # writes docs/demo.gif
#
# The key is read from $WS_API_KEY, so it never appears in the recording.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1280
Set Height 720
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms
Set PlaybackSpeed 1

Hide
Type "clear" Enter
Show

Sleep 800ms

Type "curl -s -X POST localhost:8080/search \" Enter
Type `  -H "X-API-Key: $WS_API_KEY" \` Enter
Type "  -H 'Content-Type: application/json' \" Enter
Type `  -d '{"query":"webassembly component model","limit":3,"scrape":["markdown"]}' \` Enter
Type `| jq '{query, total_ms: .timing_ms.total_ms,` Enter
Type `       results: [.results[] | {rank, title, url, track, status,` Enter
Type `                               markdown: (.markdown // "" | .[0:70] + " …")}]}'` Enter

Sleep 4s
Sleep 3s
