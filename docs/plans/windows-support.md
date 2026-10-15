# Plan: Soporte nativo de Windows para Colab CLI

```yaml
log:
  - 2026-10-14: Auditoría inicial de soporte Windows (subagente de investigación, read-only).
    Ejecución real de la suite de tests y smoke del CLI en Windows 11 (win32, Python 3.12.15 vía uv).
    Hallazgos: 3 bloqueantes, 6 altos, 7 medios, 4 bajos. 21 tests fallando / 333 pasando / 2 skips.
```

## Estado (2026-10-15)

Implementación de la rama `feat/windows-support` completada; queda la verificación manual interactiva del
usuario. SHAs de referencia en `git log --oneline main..HEAD`.

| Unidad | Alcance | Estado | Commits |
| --- | --- | --- | --- |
| U1 | `colab edit` portable (`mkstemp` + cleanup) | hecho | `188e0fe`, `b5960ae` |
| U2 | Señales portables en `ssh --rm` (sin `SIGHUP` en Windows) | hecho | `188e0fe` |
| U3 | ProxyCommand portable (comillas cmd.exe, rechazo de `%"!`/CRLF) | hecho | `188e0fe`, `cfed77c` |
| U4 | Encoding UTF-8 explícito en `open()` de `src/` | hecho | `611cc63`, `9f71b14` |
| U5 | Tests de REPL ejecutables en Windows | hecho | `611cc63` |
| U6 | Parseo robusto del help de rich | hecho | `188e0fe` |
| U7 | `colab update --install` en Windows | hecho; pendiente de prueba manual del upgrade real | `611cc63`, `9f71b14` |
| U8 | Raw TTY + resize en `colab console` (Windows) | hecho; pendiente de prueba manual | `a63ee7c`, `44ef3b9` |
| U9 | Documentación y empaquetado (README, docs, `colabrun.cmd`, classifier) | hecho (esta sesión) | — |
| U10 | CI multiplataforma (ubuntu/macos/windows) | hecho | `be8087a` |
| U11 | Integración en Windows (Git Bash documentado; sin puertos a Python, D3) | hecho (esta sesión) | — |

**Pendiente de prueba manual por el usuario:**

- **F11 (`NUL`):** `UserKnownHostsFile=NUL` ya se emite en Windows, pero falta validarlo en vivo con el
  OpenSSH nativo (`C:\Windows\System32\OpenSSH`) y con `sys.executable` en una ruta con espacios.
- **`colab console` interactivo:** eco inmediato de teclas, flechas/F, reenvío de Ctrl-C al shell remoto y
  detección de redimensionado (ver `integration/repro_windows_console_raw/MANUAL_STEPS.md`).
- **`colab update --install` en Windows:** riesgo conocido — Windows puede bloquear el reemplazo de un
  `colab.exe` en ejecución; probar también una instalación vía `uv tool install` (F6).
- **`colab auth` y `colab drivemount`:** flujos interactivos; probar en PowerShell/cmd reales.

## 1. Resumen ejecutivo

**Estado actual.** El CLI declara soporte solo para Linux y macOS (`README.md:10`), y la auditoría confirma que
Windows no está soportado de forma completa, aunque dos commits recientes ya resolvieron los bloqueantes de
importación:

- `930275e` (#135): guardó los imports de `termios`/`tty` en `console.py` (degradación a entrada por líneas) y
  añadió fallback a stdin cuando `open("/dev/tty")` falla en el prompt de Drive auth (`automation.py:106-118`).
- `d4f855d` (#158): sustituyó `select()` sobre stdin por `os.read()` bloqueante en el puente ProxyCommand de
  `colab ssh --proxy-mode` (Windows `select` solo acepta sockets).

**Lo que ya funciona hoy en Windows** (verificado en esta máquina, Windows 11 + Git Bash + uv):

- Importación e instalación: `uv sync` + `uv run colab` resuelven correctamente el entry point.
- Smoke de CLI: `colab --help`, `colab version` y `--help` de los 27 subcomandos salen con exit 0 (sin crashes
  de import).
- Estado y locking: `filelock.ReadWriteLock` (SUSTITUYE a `fcntl`) es cross-platform (`state.py:44-52`).
- Historial y logs: `history.py` usa `os.path.join` + `encoding="utf-8"` — correcto.
- `colab repl`: `prompt_toolkit` soporta Windows de forma nativa (Win32 console API); funciona en PowerShell /
  Windows Terminal reales.
- Suite de tests: 333/356 pasan en Windows sin ninguna modificación.

**Lo que NO funciona en Windows** (detallado en §3):

- `colab edit` falla siempre con `PermissionError` (tempfile con `delete=True` no reabrible en Windows).
- `colab ssh --proxy-mode --rm` falla con `AttributeError: signal.SIGHUP` (la línea ni siquiera se ejecuta).
- `ProxyCommand` generado con `shlex.join` usa comillas simples que `cmd.exe` (el shell que usa OpenSSH de
  Windows para ProxyCommand) no interpreta; con `sys.executable` en una ruta con espacios, `colab ssh` roto.
- `colab console` en TTY real degrada a entrada por líneas (necesitas Enter por cada tecla; sin redimensionado).
- `colab update --install` excluye Windows explícitamente y la detección de `uv tool` usa separadores POSIX.
- Shebangs `#!/usr/bin/env -S colab run` no tienen equivalente nativo (hay que documentar una alternativa).
- Varios `open()` sin `encoding=` leen/escriben en cp1252 en Windows (corrupción silenciosa de no-ASCII).
- 6 scripts de integración en bash y el hook git en bash requieren Git Bash (o puerto a Python).
- No existe ninguna CI de GitHub Actions (solo Cloud Build en tags); nada prueba Windows.

## 2. Resultados de la ejecución real

### 2.1 Suite de tests (`uv run pytest tests/ -q -p no:cacheprovider`)

Entorno: Windows 11, `sys.platform == "win32"`, Python 3.12.15 (`.venv` creado con `uv sync`).

**Resultado: 21 fallidos, 333 pasados, 2 skipped (54,6 s). No se colgó ningún test.**

Los 21 fallos se agrupan en 4 causas raíz:

| Causa raíz | Tests fallando | Error |
| --- | --- | --- |
| **R1. `signal.SIGHUP` no existe en Windows** — `ssh.py:545` construye la tupla `(signal.SIGHUP, ...)` FUERA del `try`, que solo captura `(ValueError, OSError)`; en Windows el `AttributeError` se escapa y `--rm` muere. | 6: `test_ssh_autocreate.py::test_proxy_mode_rm_teardown`, `test_proxy_mode_signal_handler_installation`; `test_ssh_lifecycle.py::test_proxy_mode_rm_stops_even_if_bridge_raises`, `test_proxy_bridge_routes_rm_output_to_stderr`, `test_proxy_mode_rm_teardown_idempotent`, `test_proxy_mode_rm_stops_reused_session` | `AttributeError("module 'signal' has no attribute 'SIGHUP'")` → exit 1 |
| **R2. `tempfile.NamedTemporaryFile(delete=True)` en `colab edit`** — `files.py:157`; en Windows el handle abierto impide que el editor (o `click.edit` en los tests) reabra el fichero. | 2: `test_cli.py::test_cli_edit_no_changes`, `test_cli_edit_with_changes` | `PermissionError(13, 'Permission denied')` → exit 1 |
| **R3. `prompt_toolkit.output.win32.NoConsoleScreenBufferError`** — bajo pytest stdout no es una consola Win32 real; en POSIX `prompt_toolkit` degrada a un output "dumb", en Windows lanza. Es un problema del entorno de test, no del producto. | 11: los 11 tests de `test_repl.py` | `NoConsoleScreenBufferError` en construcción de la sesión |
| **R4. Parseo frágil del help de rich** — `_extract_command_names` (test_cli.py:382) captura también palabras de descripciones envueltas (`"VM"`, `"available"`); el wrapping depende de la plataforma (longitud de las rutas por defecto en las Options, p. ej. `C:\Users\Alex/...` vs `/home/...`). No es un bug de producto. | 2: `test_cli.py::test_cli_help_commands_sorted_alphabetically`, `test_cli_help_subcommand_commands_sorted_alphabetically` | `Commands are not alphabetically sorted. Got: [..., 'VM', ..., 'available', ...]` |

Los 2 skips son los tests de modo raw de `test_console.py:45,92` (`@needs_termios`) — comportamiento esperado
en Windows; cubren el camino POSIX.

### 2.2 Smoke del CLI

`uv run colab --help` → exit 0. `uv run colab version` → `Version: 0.1.dev70+g79cd12638` (exit 0). `--help` de
los 27 subcomandos (`new`, `sessions`, `status`, `stop`, `repl`, `exec`, `console`, `ls`, `rm`, `upload`,
`download`, `edit`, `auth`, `drivemount`, `install`, `log`, `pay`, `url`, `usage`, `version`, `update`, `skill`,
`readme`, `ssh`, `run`, `whoami`, `restart-kernel`) → todos exit 0. No hay crashes de import en ninguna parte
del árbol de comandos. (No se ejecutaron comandos que asignen VMs o requieran auth.)

Dato cosmático del smoke: en el help, la opción por defecto se muestra como `C:\Users\Alex/.colab-cli-oaut…` —
`os.path.expanduser("~/.config/...")` mezcla separadores `\` y `/` (cosmético, ver F18).

### 2.3 Lo ya cubierto por los commits recientes (verificado, no repetir)

- `console.py:24-30` — imports de `termios`/`tty` con try/except ImportError.
- `console.py:168` — `getattr(signal, "SIGWINCH", None)` antes de registrarlo.
- `automation.py:106-118` — `/dev/tty` con fallback a `sys.stdin.readline()` y luego `input()`.
- `ssh.py` puente ProxyCommand — `os.read` bloqueante (sin `select`); hay test de regresión específico
  (`test_ssh.py::test_bridge_proxy_mode_pumps_stdin_to_ws`) que pasa en Windows.
- `state.py` — `filelock` en lugar de `fcntl`, con tests de locking cross-process.

## 3. Hallazgos

Severidades: **bloqueante** = el comando falla siempre en Windows; **alta** = funcionalidad degradada o rota de
forma importante; **media** = riesgo de corrupción/quirk o deuda de tests/CI; **baja** = cosmético o decisión de
diseño. Todo lo verificado contra el código real está marcado como tal; lo especulativo figura como "sin
verificar".

| # | Fichero:línea | Problema | Severidad | Solución propuesta |
| --- | --- | --- | --- | --- |
| F1 | `src/colab_cli/commands/files.py:157` | `colab edit` usa `tempfile.NamedTemporaryFile(suffix=ext)` (delete=True por defecto). En Windows el handle O_TEMPORARY impide reabrir el fichero por nombre: `click.edit(filename=...)` → `PermissionError(13)`. **Verificado** (2 tests fallan por esto). | bloqueante | Usar `tempfile.mkstemp(suffix=ext)` + `os.close(fd)`, o `NamedTemporaryFile(delete=False)`, y limpiar en `finally` con `contextlib.suppress(OSError): os.unlink()`. Mantiene el hash-compare existente. |
| F2 | `src/colab_cli/commands/ssh.py:545` | `_install_rm_signal_handlers` itera `(signal.SIGHUP, signal.SIGTERM, signal.SIGINT)`; `SIGHUP` no existe en Windows y el acceso al atributo lanza `AttributeError` FUERA del try (que solo captura `ValueError, OSError`). `colab ssh --proxy-mode --rm` muere siempre. **Verificado** (6 tests). | bloqueante | `for sig in (getattr(signal, "SIGHUP", None), signal.SIGTERM, signal.SIGINT):` + `if sig is None: continue`. Nota: en Windows, Win32-OpenSSH no envía SIGHUP a los ProxyCommand; la limpieza vía SIGTERM/SIGINT y el cierre del pipe siguen siendo suficientes. |
| F3 | `src/colab_cli/commands/ssh.py:391-408` (`_proxy_command`) | El ProxyCommand se serializa con `shlex.join()` → comillas simples POSIX (`'C:\...\python.exe' -m colab_cli.cli ssh ...`; **verificado** ejecutando `shlex.join` con el intérprete real). OpenSSH de Windows ejecuta el ProxyCommand vía `cmd.exe`, que no entiende comillas simples: con `sys.executable` en una ruta con espacios (p. ej. `C:\Program Files\Python312\`, `%LOCALAPPDATA%\...` con espacios) el comando se parte y `colab ssh` falla. Con rutas sin espacios puede funcionar por casualidad. | bloqueante | En `sys.platform == "win32"` serializar con `subprocess.list2cmdline(self_cmd)` (comillas dobles estilo cmd); en POSIX mantener `shlex.join`. Alternativa aún más robusta: generar un `.cmd` shim temporal y referenciarlo. |
| F4 | `src/colab_cli/console.py:142-190` | `colab console` en un TTY real de Windows degrada a entrada por líneas (`sys.stdin.read(1)` solo devuelve tras Enter; sin eco controlado, sin Ctrl-D, sin Ctrl-C limpio) y no hay detección de redimensionado (SIGWINCH no existe; el tamaño solo se envía en `on_open`). Funcional para piping, malo para uso interactivo. **Verificado** por lectura de código; el modo interactivo requiere prueba manual del usuario (ver §6). | alta | Ver decisión abierta D1: modo raw con `msvcrt.getwch()` + `ENABLE_VIRTUAL_TERMINAL_INPUT` (ctypes/SetConsoleMode) y polling de tamaño (hilo que consulta `os.get_terminal_size()` cada ~1 s). Mínimo aceptable: polling de redimensionado en el modo degradado. |
| F5 | `src/colab_cli/auto_update.py:48` | `is_self_install_supported()` devuelve False en Windows: `colab update --install` sale con mensaje de plataforma no soportada. No hay razón técnica (el mecanismo `pip install -U` funciona igual en Windows). | alta | Añadir `"Windows"` a la tupla y cubrir con tests parametrizados por plataforma. |
| F6 | `src/colab_cli/auto_update.py:242` | La detección de instalación uv usa `"/uv/tools/" in sys.executable`; en Windows `sys.executable` lleva `\` (`%APPDATA%\uv\tools\colab-cli\...`), así que aunque se habilite F5, detectaría uv mal y usaría el fallback pip. **Verificado** por lectura; el path real de uv en Windows conviene confirmarlo en la unidad. | alta | Normalizar: `("/uv/tools/" in exe.replace("\\\\", "/")) or ("uv\\\\tools\\\\" in exe)` — mejor: comparar el path normalizado con `Path(sys.executable).parts`. |
| F7 | `src/colab_cli/commands/execution.py:188` | `open(file, "r")` sin `encoding=` al leer el script de `colab exec` (rama no-.ipynb): en Windows decodifica cp1252 → `UnicodeDecodeError` o mojibake con scripts UTF-8 que contengan acentos/emoji. (La rama .ipynb, `execution.py:176`, sí usa `encoding="utf-8"`.) **Verificado** por lectura. | alta | `open(file, "r", encoding="utf-8")`. |
| F8 | `src/colab_cli/commands/run.py:26,278`; `docs/05_run_command.md`; `examples/finetune_run.py` | El flujo estrella de `colab run` — shebang `#!/usr/bin/env -S colab run` — no tiene equivalente en Windows: `cmd`/PowerShell no ejecutan shebangs. Sin alternativa documentada, la característica principal queda inaccesible en Windows. | alta | Documentar en README + docs/05: en Windows usar `colab run script.py args...` directamente, o crear un shim `colabrun.cmd` (`@colab run %*`) / una función PowerShell y añadirlo al PATH. `_strip_shebang` (run.py) ya tolera scripts sin shebang. |
| F9 | `README.md:10`; `pyproject.toml:18` | README declara "Linux and macOS only. Windows is not supported" mientras pyproject se anuncia `Operating System :: OS Independent`. Contradicción que confunde a usuarios y bloquea la adopción. | alta | Tras implementar las unidades de este plan, actualizar README (sección de plataformas + instrucciones Windows: `uv tool install`, OpenSSH opcional, Git Bash para integración) y alinear el classifier. |
| F10 | `src/colab_cli/auth.py:103,146`; `src/colab_cli/state.py:78,84` | `open()` sin `encoding=` para el OAuth client config, el token (`creds.to_json()`) y `sessions.json`/`settings.json`. Con ensure_ascii los JSON son mayormente ASCII-safe, pero nombres de sesión o configs con no-ASCII → cp1252 mojibake o `UnicodeDecodeError`. **Verificado** por lectura. | media | Añadir `encoding="utf-8"` en las 4 llamadas (auditar el resto de `open()` de `src/` con la misma lupa). |
| F11 | `src/colab_cli/commands/ssh.py:420` | `UserKnownHostsFile=/dev/null` en los args de ssh. El OpenSSH nativo de Windows (System32\OpenSSH) interpreta `/dev/null` como ruta relativa a la unidad actual en algunos escenarios (creación de `C:\dev\...`) — comportamiento reportado históricamente, **sin verificar** en esta máquina. Con Git Bash/Cygwin ssh funciona. | media | Verificar en vivo; si falla, usar `NUL` en Windows (`ssh -o UserKnownHostsFile=NUL`) con branching por plataforma. |
| F12 | `src/colab_cli/console.py:142-190` (parte de F4, se lista aparte por alcance) | Detección de resize del terminal ausente en Windows. Mínimo viable sin raw-TTY: hilo daemon que compara `os.get_terminal_size()` cada segundo y reenvía el payload JSON. | media | Incluir el polling de tamaño como primer paso de la unidad U8, independiente del modo raw. |
| F13 | `tests/test_repl.py` (11 tests) | En Windows bajo pytest, `prompt_toolkit` lanza `NoConsoleScreenBufferError` (stdout capturado no es consola Win32). En POSIX estos tests pasan porque prompt_toolkit degrada. Los tests deben inyectar `DummyOutput`/`DummyInput` de `prompt_toolkit.output/win32`. **Verificado** (ejecución real). | media | Fixture en `tests/conftest.py` que parchee `PromptSession.output`/`input` con dummies, o construir el `ColabREPL` en tests con `create_app_session` parcheado. No tocar el producto. |
| F14 | `tests/test_cli.py:382-426` | `_extract_command_names` captura palabras de descripciones envueltas; el resultado depende del wrapping de rich (plataforma/anchura). **Verificado** (ejecución real, causa R4). | media | Parsear solo las líneas de la sección `+- Commands ----+` cuyo primer campo no empiece por espacio/continuación, o comparar contra la lista canórica registrada en `cli.py`. |
| F15 | `integration/*/test.sh` (6 scripts: `repro_bundled_oauth`, `repro_piped_console`, `repro_plot_redirection`, `repro_run_command`, `repro_ssh`, `repro_variable_persistence`) | Bash-only (y `$HOME`, `command -v`, pipes POSIX). En Windows solo ejecutables vía Git Bash/WSL (`uv run bash integration/...` funciona si `bash` está en PATH — **verificado** que Git Bash está presente en esta máquina). | media | Corto plazo: documentar en `integration/README.md` que en Windows se requiere Git Bash. Medio plazo (decisión D3): portar los runners a Python (`integration/repro_x/test.py`) invocando el CLI con `subprocess`, y dejar el `.sh` como wrapper. |
| F16 | `.githooks/pre-commit` (shebang bash) + `.pre-commit-config.yaml` | El hook wrapper es bash; pre-commit en sí soporta Windows, pero `uv run pytest`/`ruff` como hooks `language: system` requieren uv en PATH. Funciona en Git Bash; en PowerShell puro depende del core.hooksPath y del shell usado por git. | media | Probar `git commit` en PowerShell con Git for Windows; documentar el requisito o sustituir el wrapper por un hook Python. Verificar antes de tocar. |
| F17 | `src/colab_cli/auth.py:123-150` | El token OAuth (`token.json`) se escribe sin permisos restrictivos (en POSIX ni `chmod 0o600`; en Windows sin ACL explícita hereda las del perfil, que por defecto ya son privadas). Riesgo bajo en Windows, relevante al anunciar soporte multiplataforma. | media | En POSIX, `os.chmod(TOKEN_CONFIG_PATH, 0o600)` tras escribir. En Windows, no hacer nada (ACL del perfil) o documentar. |
| F18 | `src/colab_cli/common.py:40,195`; `src/colab_cli/state.py:91,115`; `src/colab_cli/auth.py:54`; `src/colab_cli/history.py:22` | Rutas de estado bajo `~/.config/colab-cli` (expanduser): en Windows resuelven a `%USERPROFILE%\.config\colab-cli`. Funciona, pero no es el lugar idiomático (`%APPDATA%`) y mezcla separadores en el help (`C:\Users\X/...`). | baja | Decisión D2: mantener `~/.config` (identidad cross-platform, override con `--config`) o migrar a `platformdirs`. Recomendación en §4. |
| F19 | `src/colab_cli/cli.py:57` (help de `--client-oauth-config`) | Cosmético: el help muestra `C:\Users\Alex/.colab-cli-oaut…` (separadores mezclados por `expanduser` + sufijo literal). **Verificado** en el smoke. | baja | Se corrige solo si se adopta la migración de F18; si no, dejarlo. |
| F20 | `src/colab_cli/commands/files.py:157` + `click._termui_impl.get_editor` | Editor por defecto en Windows: click hace fallback a `notepad` (verificado en click 8.4.1) y `subprocess.Popen(shlex.split(editor) + [file])`. `notepad` guarda CRLF: el fichero re-subido a Colab llevará CRLF (Python/IPython lo toleran; diffs remotos cambian). `$EDITOR`/`VISUAL` ya se respetan. | baja | No cambiar código; documentar que se puede fijar `EDITOR`. Opcionalmente normalizar CRLF→LF antes de re-subir en `edit`. |
| F21 | `.github/` (no existe) | No hay ninguna GitHub Actions workflow: la única CI es Cloud Build en tags (build/publish, sin tests). Nada ejecuta pytest en ninguna plataforma, mucho menos en Windows. | alta (procesal) | Añadir `.github/workflows/ci.yml` con matriz `ubuntu-latest`, `macos-latest`, `windows-latest` (Python 3.12/3.13) ejecutando `uv sync && uv run pytest tests/ -q && uv run ruff check .`. |

Resumen por severidad: **3 bloqueantes** (F1–F3), **7 altas** (F4–F9, F21), **7 medias** (F10–F16), **4 bajas**
(F17–F20).

## 4. Decisiones abiertas que requieren criterio del responsable

> **Decisiones tomadas (2026-10-14):** D1 → (b) en dos pasos (polling de tamaño + raw con `msvcrt` y VT input).
> D2 → mantener `~/.config/colab-cli`. D3 → documentar Git Bash; sin puertos a Python por ahora. D4 → según
> recomendación (F2 + test de teardown por cierre de pipe). D5 → no normalizar CRLF; documentar `EDITOR`.
> Orden: lote A (U1+U2+U3+U6) y lote B (U4+U5+U7) en paralelo con ficheros disjuntos → U8 → U10 + U9 + U11.

**D1 — Modo raw TTY de `colab console` en Windows.**
Opciones: (a) mantener el modo degradado actual (solo líneas + Enter); (b) implementar raw con
`msvcrt.getwch()` + VT input (`SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_INPUT)`) reutilizando el mismo bucle
ws.send({"data": ch}); (c) reescribir `console` sobre `prompt_toolkit` (que ya resuelve input/output/resize en
Windows y POSIX). *Recomendación: (b) en dos pasos — primero el polling de redimensionado (F12, barato y sin
riesgo), luego msvcrt+VT como unidad U8. (c) es la solución más limpia a largo plazo pero reescribe el camino
POSIX que hoy funciona y está testado; prohibido mezclar. Trade-off de (b): msvcrt no distingue bien secuencias
de escape extendidas sin VT input habilitado, y hay que mapear Ctrl-D manualmente (0x04 llega como tecla, no
como EOF).*

**D2 — Rutas de estado en Windows: `~/.config` vs `%APPDATA%`.**
*Recomendación: mantener `~/.config/colab-cli` (F18 sin acción inmediata). Razones: comportamiento idéntico en
las tres plataformas, el flag global `--config` ya permite override, y migrar rompería a quien usa ambos SO con
el mismo home sincronizado. Revisar si llega una petición de usuario; entonces usar `platformdirs` con
migración automática del estado existente. Trade-off: no-idiomático en Windows, pero cero riesgo.*

**D3 — Tests de integración bash: Git Bash vs puerto a Python.**
*Recomendación: documentar Git Bash ahora (es el mínimo viable; los 6 scripts corren sin cambios) y portar a
Python solo los 2 rápidos (`repro_bundled_oauth`, `repro_piped_console`) si se quieren en CI de Windows. Los
4 restantes allocan VMs reales y NO deben entrar en CI automática bajo ninguna plataforma. Trade-off del
puerto: doble mantenimiento transitorio; ventaja: CI reproducible sin bash.*

**D4 — Comportamiento de `--rm` sin SIGHUP en Windows.**
Win32-OpenSSH no envía SIGHUP al ProxyCommand al desconectar; la limpieza dependerá del cierre del pipe (stdin
EOF) y de SIGTERM/SIGINT. *Recomendación: aplicar F2 y añadir un test que verifique que el teardown corre por
la ruta de cierre de pipe (ya existe el pump que cierra el ws); no intentar emular SIGHUP. Verificación manual
del usuario necesaria (§6).*

**D5 — ¿Corregir también el CRLF de `colab edit` en Windows (F20)?**
*Recomendación: no en la primera pasada. Normalizar `\r\n` antes de re-subir es una línea, pero cambia bytes
para usuarios POSIX también si su editor escribe CRLF deliberadamente; mejor documentar. Requiere criterio si
se quiere determinismo de hashes.*

## 5. Unidades de implementación independientes

Convención TDD: en cada unidad se escriben primero los tests, se verifica que fallan en Windows (y que siguen
pasando en POSIX), y luego se implementa. Todas ejecutan `uv run pytest tests/ -q` y `uv run ruff check .`
como criterio global de aceptación.

| Unidad | Alcance | Ficheros | Tests primero (TDD) | Criterios de aceptación | Dependencias | Tamaño |
| --- | --- | --- | --- | --- | --- | --- |
| **U1. `colab edit` portable** | Sustituir `NamedTemporaryFile(delete=True)` por `mkstemp` + cleanup en `finally`. | `src/colab_cli/commands/files.py`, `tests/test_cli.py` | Test que ejecute el flujo real de `edit` sin mockear el tempfile (o con spy) y verifique que el fichero es reabrible en Windows (p. ej. que `click.edit` recibe una ruta que existe y no está abierta); regression: `test_cli_edit_no_changes/with_changes` pasan en win32. | Los 2 tests hoy rojos en verde en Windows; sin cambios de comportamiento POSIX (hash-compare intacto; cleanup sin dejar tmp). | Ninguna. Paralelizable con U2, U5, U6, U7, U9, U10. | S |
| **U2. Señales portables en ssh `--rm`** | `getattr(signal, "SIGHUP", None)` + filtro de `None` en `_install_rm_signal_handlers`. | `src/colab_cli/commands/ssh.py`, `tests/test_ssh_lifecycle.py`, `tests/test_ssh_autocreate.py` | Los 6 tests hoy rojos (R1) deben pasar sin modificar los tests (son la regresión); añadir 1 test que force `signal.SIGHUP = None` simulando win32 y verifique que el handler se instala solo con SIGTERM/SIGINT. | 6 tests R1 en verde; teardown `--rm` verificado por la ruta de cierre de pipe. | Ninguna. | S |
| **U3. ProxyCommand portable (comillas por plataforma)** | Serializar el self-cmd con `subprocess.list2cmdline` en win32 y `shlex.join` en POSIX. | `src/colab_cli/commands/ssh.py` (mismo fichero que U2 — secuenciar o coordinar), `tests/test_ssh.py` | Test parametrizado: con un `sys.executable` simulado con espacios, en win32 el ProxyCommand generado usa comillas dobles y se re-parsea bien con `shlex.split(..., posix=False)`/`subprocess.list2cmdline` round-trip; en POSIX sigue siendo `shlex.join`. | Round-trip de quoting verificado para rutas con espacios y con backslashes; tests POSIX existentes sin cambios. | Mismo fichero que U2 → hacer después de U2 o en la misma sesión. | M |
| **U4. Encoding UTF-8 explícito** | Añadir `encoding="utf-8"` a todos los `open()` de texto de `src/` sin él (`execution.py:188`, `auth.py:103,146`, `state.py:78,84`). | `src/colab_cli/commands/execution.py`, `src/colab_cli/auth.py`, `src/colab_cli/state.py` | Test de carácter no-ASCII: escribir un script .py con acentos y verificar que `colab exec` lo lee bien con locale cp1252 forzado (`PYTHONUTF8=0`); ídem round-trip de `sessions.json` con nombre de sesión no-ASCII. | Suite verde con `PYTHONUTF8=0` y con `PYTHONUTF8=1`; `rg -n "open\\(" src/colab_cli` sin llamadas de texto sin encoding. | Ninguna. Paralelizable (ficheros disjuntos). | S/M |
| **U5. Tests de REPL ejecutables en Windows** | Fixture en conftest que inyecte `DummyOutput`/`DummyInput` de prompt_toolkit para los tests de `ColabREPL`. | `tests/conftest.py`, `tests/test_repl.py` | No aplicable (son los tests los que se arreglan): verificar que los 11 tests fallan hoy y pasan tras el fixture, sin tocar `src/`. | 11 tests R3 en verde en Windows; en POSIX sin cambios. | Ninguna. | S |
| **U6. Parseo robusto del help** | Reescribir `_extract_command_names` para extraer solo la columna de comandos de la sección Commands (o comparar contra la lista canórica de `cli.py`). | `tests/test_cli.py` | No aplicable (arreglo de test): los 2 tests R4 pasan en Windows y Linux. | 2 tests R4 en verde; el test sigue detectando de verdad un orden no alfabético (probar mutando la lista). | Ninguna. | S |
| **U7. `colab update --install` en Windows** | Añadir "Windows" a `is_self_install_supported` + detección de uv por `Path.parts`; documentar el mensaje. | `src/colab_cli/auto_update.py`, `src/colab_cli/commands/utility.py`, `tests/test_update.py` | Tests parametrizados por `platform.system()` mockeado (incluye "Windows") para soporte/no-soporte y para la detección de rutas uv con `\\`. | `colab update --install` no rechaza Windows; pip fallback correcto cuando no es uv-tool. Verificación live del upgrade real la hace el usuario (§6). | Ninguna. | S/M |
| **U8. Raw TTY + resize en `colab console` (Windows)** | Paso 1: hilo de polling de `os.get_terminal_size()` (todas las plataformas ganan resize en modo degradado). Paso 2 (según D1): en win32 + TTY, entrada raw con `msvcrt` + VT input sustituyendo el lector `sys.stdin.read(1)`. | `src/colab_cli/console.py`, `tests/test_console.py` | Tests de polling: tamaño inicial y reenvío al cambiar (mockeando `os.get_terminal_size`); tests de degradación existentes intactos; para msvcrt, test con mock de `msvcrt.getwch` que verifique el mapeo de teclas (incluida la emulación de Ctrl-D/0x04). | Modo piped sin regresión (los tests actuales en verde); en TTY real de Windows el usuario valida manualmente (§6). | Requiere decisión D1 del responsable antes del paso 2; el paso 1 es independiente. | L |
| **U9. Documentación y empaquetado** | README (plataformas, instalación Windows, shebang alternativo, OpenSSH opcional, Git Bash para integración), docs/05 (run sin shebang), docs/06 (ssh en Windows, nota sobre `/dev/null`), alinear classifier de `pyproject.toml`. Entrada en `CHANGELOG` NO (solo en release). | `README.md`, `docs/02…06` según toque, `pyproject.toml`, `examples/` (añadir `colabrun.cmd`) | No hay tests de producto; `test_readme.py` y `test_version.py` deben seguir en verde (el README va embebido). | Docs coherentes con el estado post-unidades; classifier sin contradicción. | Tras U1–U8 (documenta su resultado). | S |
| **U10. CI multiplataforma** | Nueva `.github/workflows/ci.yml`: matriz {ubuntu, macos, windows} × {3.12, 3.13}, `uv sync`, `pytest`, `ruff check`. Excluir explícitamente los tests de integración con VMs. | `.github/workflows/ci.yml` | No aplicable (infra). | Workflow en verde en los 3 SO; es la red de seguridad de todas las demás unidades. | Ninguna; conviene activarla lo antes posible (antes de U1). | S |
| **U11. Integración en Windows** | Documentar Git Bash en `integration/README.md` (F15/D3); opcionalmente portar a Python los 2 tests rápidos. | `integration/README.md`, `integration/repro_bundled_oauth/`, `integration/repro_piped_console/` | Los puertos Python se validan corriendo ambos runners y comparando exit codes con los .sh. | README de integración con sección Windows; los 2 rápidos ejecutables con y sin bash. | Decisión D3; para los puertos Python, después de U10. | M |

**Grafo de dependencias:** U10 debe ir primero (red de seguridad). U1, U2, U4, U5, U6, U7, U10 son
independientes y paralelizables (ficheros disjuntos) — salvo U3, que comparte `commands/ssh.py` con U2 y debe
venir después (o fusionarse en la misma sesión). U8 depende de la decisión D1. U9 cierra el plan tras el resto.
U11 depende de D3 y, para su parte Python, de U10.

## 6. Estrategia de verificación

**Automático (cada unidad):**
- `uv run pytest tests/ -q -p no:cacheprovider` en Windows: objetivo 0 fallos (los 2 skips de termios son
  esperados) y sin regresiones en POSIX (CI de U10).
- `uv run ruff check . && uv run ruff format --check .`.
- `uv run colab --help` + `--help` de los 27 subcomandos como smoke post-cambio (exit 0 en los 3 SO).

**CI (U10):** matriz `ubuntu-latest` / `macos-latest` / `windows-latest`, Python 3.12 y 3.13, solo unit tests.
Actualización 2026-10-15: por decisión del usuario la CI solo ejecuta `windows-latest` (3.12 y 3.13); Linux y macOS ya no se validan en CI.
Nada que asigne VMs.

**Manuales que SOLO el usuario puede ejecutar en Windows (interactivos; el agente no puede):**
1. `colab auth` (flujo copiar-pegar; también confirma el flujo OAuth2 remoto en PowerShell/cmd).
2. `colab repl` interactivo: teclas Enter/Escape-Enter, Ctrl-C, `/quit`, streaming de salida.
3. `colab console` interactivo tras U8: eco inmediato de teclas, Ctrl-D, redimensionado de ventana (el tamaño
   debe reenviarse al backend).
4. `colab drivemount` (prompt "Press Enter" tras conceder permisos en el navegador).
5. `colab ssh` interactivo y `colab ssh --proxy-mode` desde `~/.ssh/config` de Windows OpenSSH
   (`C:\Users\<u>\.ssh\config`), con el ProxyCommand generado por U3 — validar en particular con un
   `sys.executable` que contenga espacios, y comprobar el comportamiento de `UserKnownHostsFile=/dev/null`
   (F11) antes de decidir si hay que usar `NUL`.
6. `colab update --install` real tras U7 (incluida una instalación vía `uv tool install` para validar F6).
7. `colab edit` tras U1 con notepad y con `$EDITOR` apuntando a un editor de terminal (p. ej. vim en Git Bash).

**Integración live (prohibida en CI, con cleanup obligatorio según AGENTS.md):**
- `uv run bash integration/repro_piped_console/test.sh` y `uv run bash integration/repro_bundled_oauth/test.sh`
  desde Git Bash como validación rápida post-U8/U1.
- Al terminar cualquier prueba live: `colab sessions` y limpieza de huérfanos con
  `python -c "from colab_cli.common import state; state.client.unassign('<endpoint>')"` (regla 19 de AGENTS.md).
