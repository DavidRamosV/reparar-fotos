# Seguridad

## Qué hace y qué no hace la herramienta

- **Corre por completo en tu navegador.** La página declara una política de seguridad de contenido (*Content-Security-Policy*) que bloquea cualquier conexión de red (`connect-src 'none'`) y no carga scripts, estilos ni imágenes de otros sitios.
- **No funciona dentro de marcos de otros sitios.** Si otra página la incrusta (*iframe*), la herramienta se oculta y ofrece abrirla en su propia pestaña, para que nadie pueda disfrazarla ni superponerle elementos engañosos.
- **No usa cookies**, almacenamiento del navegador ni analíticas.
- **Lee solo los archivos que eliges** y escribe solo en la carpeta que autorizas o en el ZIP que descargas. Nunca reemplaza archivos existentes.
- **Las fotos dañadas o manipuladas solo pueden producir un error**, que se reporta como «Sin reparar». Para que un archivo hecho a propósito no agote la memoria, hay límites de 300 MB por archivo y de 250 megapíxeles por foto.
- **Los nombres de archivo se ajustan** para que sean válidos en Windows, macOS y Linux, de modo que al descomprimir el ZIP ninguno pueda escribir fuera de la carpeta de resultados.

## Versiones con soporte

Solo la versión más reciente publicada en *Releases*, que es también la versión en línea.

## Cómo reportar una vulnerabilidad

Usa el botón **Report a vulnerability** de la pestaña **Security** de este repositorio. Así el reporte es privado hasta que haya una corrección. No abras un *issue* público para problemas de seguridad y no adjuntes fotos personales.

## Cómo verificar tu descarga

Cada versión en *Releases* incluye la huella SHA-256 de `reparar_fotos.html`. Para comprobarla, calcula la huella del archivo que descargaste y compárala:

- **Linux:** `sha256sum reparar_fotos.html`
- **macOS:** `shasum -a 256 reparar_fotos.html`
- **Windows (PowerShell):** `Get-FileHash reparar_fotos.html`

Si no coincide, no uses ese archivo.

## Versión en línea

La versión en <https://davidramosv.github.io/reparar-fotos/> es el mismo archivo `reparar_fotos.html` de la versión más reciente en *Releases*. Se publica únicamente al publicar una versión, con el flujo [`.github/workflows/pages.yml`](.github/workflows/pages.yml), que comprueba que el archivo adjunto sea idéntico al del repositorio en esa etiqueta y anota su huella SHA-256 en el registro de la ejecución.

GitHub solo entrega la página: como cualquier sitio web, puede registrar la visita (por ejemplo, la dirección IP), pero no recibe tus fotos ni sus nombres.

Para comprobar que la página publicada coincide con la huella de la versión:

- **Linux:** `curl -s https://davidramosv.github.io/reparar-fotos/ | sha256sum`
- **macOS:** `curl -s https://davidramosv.github.io/reparar-fotos/ | shasum -a 256`
- **Windows (PowerShell):** `Invoke-WebRequest https://davidramosv.github.io/reparar-fotos/ -UseBasicParsing -OutFile pagina.html; Get-FileHash pagina.html`
