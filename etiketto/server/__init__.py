"""The print server: a REST API and MCP for printing labels without a window.

    etiketto serve [--host 127.0.0.1] [--port 8790] [--mcp]   HTTP (docs/server.md)
    etiketto mcp                                              MCP on stdin/stdout

Nothing here imports GTK: rendering uses Pango and Cairo (render.py), as
the command line does. The parts:

- paths.py: the labels folder; label and data files are only ever read
  from inside it
- requests.py: the JSON of a preview or print request, checked
- service.py: what the API can do, the same for HTTP and MCP
- jobs.py: print jobs in a queue per printer, one at a time
- http.py: the HTTP server, its routes and its security checks
- mcp.py: the Model Context Protocol (stdio and Streamable HTTP)
- openapi.py: the description of the REST API
"""
