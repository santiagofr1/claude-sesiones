# claude-sesiones

**Gestor de sesiones de Claude Code: lista, busca, manda a la papelera y limpia lo que sobra.**

Claude Code guarda cada sesión como un archivo dentro de `~/.claude/projects`, y con el tiempo eso se acumula
en cientos de MB. No hay forma de borrar una sesión suelta, ni de sacar las que quedaron de proyectos que ya no
existen. Y el plugin ruflo deja carpetas `.claude-flow` repartidas por todo el disco. `ses` te deja ver qué hay,
borrar con cuidado (todo va a una papelera recuperable) y limpiar la basura sin tener que buscar archivos a mano.

## Qué hace

- Lista y busca sesiones por texto, proyecto o tamaño.
- Muestra el detalle de una sesión y el comando para retomarla.
- Borra sesiones a una papelera recuperable.
- Restaura lo que está en la papelera.
- Limpia basura: sesiones vacías o rotas, artefactos huérfanos y carpetas de proyecto vacías.
- Limpia lo viejo: sesiones sin uso hace N días, memorias de proyecto sin uso y carpetas `.claude-flow`.
- Vacía la papelera de forma definitiva.

## Instalación

Requiere Python 3.8 o superior. No tiene dependencias externas.

### Debian / Ubuntu y derivadas: repo apt (recomendado)

Agregás el repo una vez y después `ses` se actualiza con el resto del sistema:

```sh
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://santiagofr1.github.io/apt/key.asc | sudo gpg --dearmor -o /etc/apt/keyrings/santiagofr1.gpg
echo "deb [signed-by=/etc/apt/keyrings/santiagofr1.gpg] https://santiagofr1.github.io/apt stable main" | sudo tee /etc/apt/sources.list.d/santiagofr1.list
sudo apt update
sudo apt install claude-sesiones
```

### Desde el código

```
git clone https://github.com/santiagofr1/claude-sesiones.git ~/Git/claude-sesiones
ln -s ~/Git/claude-sesiones/ses.py ~/.local/bin/ses
```

## Comandos

### `list`

Lista las sesiones, las más nuevas primero, con el total al final. El texto busca en el título, el proyecto y el id.

- `--project SUBSTR`: solo sesiones cuyo proyecto contiene `SUBSTR`.
- `--orphans`: solo sesiones cuyo proyecto ya no existe.
- `--sort date|size`: ordena por fecha (default) o por tamaño.
- `--json`: salida en JSON en vez de tabla.

```
ses list arduino --sort size   # busca "arduino", las más pesadas primero
ses list --orphans             # sesiones de proyectos que ya no existen
ses list --project notas       # solo las del proyecto que contenga "notas"
```

### `show`

Muestra el detalle de una sesión: proyecto, fechas, tamaño con artefactos, cantidad de mensajes, primeros y
últimos mensajes del usuario, y el comando `claude --resume` para retomarla.

- `<id>`: uuid completo o prefijo de 4 caracteres o más.

```
ses show 3f2a                  # detalle de la sesión que empieza con 3f2a
```

### `delete`

Manda una o más sesiones a la papelera. Si era la última sesión de su proyecto, la memoria del proyecto va con ella.

- `<id>`: uuid o prefijo de 4 caracteres o más. Si el prefijo es ambiguo, no hace nada.

```
ses delete 3f2a 9c1d           # a la papelera, las dos
```

### `restore`

Sin argumentos lista lo que hay en la papelera. Con un `<id>` devuelve la sesión a su lugar, con su memoria si
la trajo. No pisa nada: si el destino ya existe, no toca nada.

- `<id>`: uuid o prefijo. Las carpetas `.claude-flow` aparecen como `[flow]`.

```
ses restore                    # lista la papelera
ses restore 3f2a               # vuelve la sesión (y su memoria) a su lugar
```

### `junk`

Busca basura y la manda a la papelera: sesiones de 0 bytes, archivos sin ninguna línea JSON válida, sesiones sin
mensajes, artefactos huérfanos (carpeta de sesión sin su `.jsonl`) y carpetas de proyecto vacías. No toca las
carpetas `.claude-flow`.

- `--dry-run`: muestra la lista y no toca nada.

```
ses junk --dry-run             # mira qué basura hay, sin tocar nada
ses junk                       # manda la basura a la papelera
```

### `old`

Manda a la papelera las sesiones sin uso hace más de `<dias>`, las memorias de proyecto sin uso, y **todas** las
carpetas `.claude-flow` bajo tu home, sin importar su antigüedad. Las memorias de proyectos sin sesiones van a
`_huerfanos`.

- `<dias>`: entero mayor a 0.
- `--dry-run`: lista lo que se movería y no toca nada.
- `--project SUBSTR`: filtra sesiones y memorias por proyecto. No filtra las `.claude-flow`.

```
ses old 30 --dry-run           # sesiones de más de 30 días, solo mostrar
ses old 90 --project rtop      # las viejas del proyecto que contenga "rtop"
```

### `empty`

Borra la papelera de forma definitiva, sin preguntar.

- `--dry-run`: muestra cuánto se borraría y no toca nada.

```
ses empty --dry-run            # cuánto ocupa la papelera, sin borrar
ses empty                      # borra la papelera para siempre
```

## Cómo te cuida

- La papelera está en `~/.claude/ses-papelera`. Nada se borra de forma definitiva salvo con `ses empty`.
- No toca sesiones modificadas en los últimos 10 minutos, por si Claude Code las está usando.
- `junk`, `old` y `empty` aceptan `--dry-run` para ver primero qué harían.
- La memoria de un proyecto se va sola con su última sesión, y vuelve con `restore`.
- La memoria del proyecto del home está protegida y no se toca.
- Verifica las rutas antes de mover o borrar: solo opera dentro de `projects`, `file-history`, `session-env` y
  la papelera. No sigue symlinks.

## Configuración

Variables de entorno opcionales:

| Variable | Default | Qué hace |
|---|---|---|
| `CLAUDE_HOME` | `~/.claude` | Carpeta raíz de Claude Code. La papelera queda en `$CLAUDE_HOME/ses-papelera`. |
| `SES_ACTIVA_MIN` | `10` | Minutos desde la última modificación para considerar una sesión activa. Las activas no se tocan. |
| `SES_PROTEGER_HOME` | `1` | Con `0`, deja de proteger la memoria del proyecto del home. |
| `SES_FLOW_ROOT` | `$HOME` | Raíz donde `old` busca las carpetas `.claude-flow`. |
| `NO_COLOR` | (sin definir) | Si tiene algún valor no vacío, desactiva los colores de la salida. |

## Ejemplo de salida

Ejemplo de `ses list` con datos inventados:

```
ID        MODIFICADA        TAMANO    MSJ  PROYECTO                 TITULO
3f2a9c10  2026-11-20 18:42  12.4 MB   236  ~/Git/proyecto-ejemplo   refactor del colector de datos
9c1d7e54  2026-11-18 09:15  340.2 KB   41  ~/Git/viejo-experimento  [borrado] probar el parser
b07e4a21  2026-11-21 21:03  1.8 MB     97  ~/Git/app-demo           [activa] pantalla de exportar
c5f08d3e  2026-10-02 11:27  88 B        2  ~/Downloads              prueba rapida

4 sesiones, 14.6 MB en total
```

Las marcas `[borrado]` (el proyecto ya no existe) y `[activa]` (modificada hace menos de `SES_ACTIVA_MIN`) aparecen
al final del título.

## Tests

```
python3 -m unittest discover -s tests
```

Los tests usan carpetas temporales con un `CLAUDE_HOME` propio. Nunca tocan tu `~/.claude` real.
