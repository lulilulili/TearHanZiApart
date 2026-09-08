@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=python
where python >nul 2>nul
if errorlevel 1 set PY=py
%PY% -V >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3 not found. Please install it from https://www.python.org/
  pause
  exit /b 1
)
%PY% -c "import os,json;open('StrokeLab/fontList.js','w',encoding='utf-8').write('window.FONT_LIST='+json.dumps(sorted([f for f in os.listdir('Fonts') if f.lower().endswith(('.ttf','.otf'))]))+';')"
echo CharStrokeLab starting... close this window to stop the server.
%PY% -c "import http.server,socketserver,webbrowser,functools;socketserver.ThreadingTCPServer.allow_reuse_address=True;h=functools.partial(http.server.SimpleHTTPRequestHandler,directory='.');s=socketserver.ThreadingTCPServer(('127.0.0.1',0),h);u='http://localhost:{}/StrokeLab/charStrokeLab.html'.format(s.server_address[1]);print('  '+u);webbrowser.open(u);s.serve_forever()"
