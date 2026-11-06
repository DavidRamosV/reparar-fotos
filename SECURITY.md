# Seguridad

## Qué hace y qué no hace la herramienta

- **Corre por completo en tu navegador.** La página declara una política de seguridad de contenido (*Content-Security-Policy*) que bloquea cualquier conexión de red (`connect-src 'none'`) y no carga scripts, estilos ni imágenes de otros sitios.
- **No usa cookies**, almacenamiento del navegador ni analíticas.
- **Lee solo los archivos que eliges** y escribe solo en la carpeta que autorizas o en el ZIP que descargas. Nunca reemplaza archivos existentes.
- **Las fotos dañadas o manipuladas solo pueden producir un error**, que se reporta como «Sin reparar». Para que un archivo hecho a propósito no agote la memoria, hay límites de 300 MB por archivo y de 250 megapíxeles por foto.
- **Los nombres de archivo se ajustan** para que sean válidos en Windows, macOS y Linux, de modo que al descomprimir el ZIP ninguno pueda escribir fuera de la carpeta de resultados.

## Versiones con soporte

Solo la versión más reciente publicada en *Releases*.

## Cómo reportar una vulnerabilidad

Usa el botón **Report a vulnerability** de la pestaña **Security** de este repositorio. Así el reporte es privado hasta que haya una corrección. No abras un *issue* público para problemas de seguridad y no adjuntes fotos personales.

## Cómo verificar tu descarga

Cada versión en *Releases* incluye la huella SHA-256 de `reparar_fotos.html`. Para comprobarla, calcula la huella del archivo que descargaste y compárala:

- **Linux:** `sha256sum reparar_fotos.html`
- **macOS:** `shasum -a 256 reparar_fotos.html`
- **Windows (PowerShell):** `Get-FileHash reparar_fotos.html`

Si no coincide, no uses ese archivo.
