# vx-dga-l-vas

Paquete Debian para Vitalinux que instala Vitalinux Autoregistration Server (VAS) de forma global.

## Descripcion

Este paquete instala VAS en el sistema para su despliegue mediante Migasfree en entornos Vitalinux. VAS expone una API HTTP basada en FastAPI, almacena clientes en SQLite y genera automáticamente el archivo computers.json de Veyon para que los clientes VAC puedan sincronizar la configuración.

Adicionalmente, VAS puede sincronizar automáticamente los networkobjects en un Veyon Master mediante veyon-cli.

## Requisitos

- Sistema Vitalinux compatible con paquetes Debian.
- Python 3 en tiempo de ejecución.
- Dependencias Python de sistema:
  - python3-fastapi
  - python3-uvicorn
  - python3-pydantic
- systemd para gestión del servicio.

- Sincronización con VEYON depende de tener disponible veyon-cli instalado

## Informacion del Paquete

- Nombre: vx-dga-l-vas
- Version: 0.4-3
- Arquitectura: all
- Mantenedor: Gabriel Navia <correos@gabrielnav.es>
- Licencia: GPL-3.0+

## Archivos incluidos

- usr/lib/vas/vas.py - Aplicación principal FastAPI
- usr/lib/vas/database.py - Capa de persistencia SQLite y generación de JSON
- usr/bin/vas - Script wrapper de arranque
- etc/vas/vas.conf - Configuración editable del servicio
- lib/systemd/system/vas.service - Unidad systemd de VAS
- debian/postinst - Inicialización de datos y habilitación/arranque del servicio
- debian/prerm - Parada y deshabilitación del servicio al eliminar
- debian/postrm - Limpieza de datos persistentes en purge

## Funcionamiento de VAS

VAS implementa un flujo simple y robusto de autoregistro:

1. Arranque
   - systemd ejecuta usr/bin/vas.
   - El wrapper carga etc/vas/vas.conf y levanta Uvicorn sobre vas:app.
   - La aplicación inicializa la base de datos SQLite y la versión local.

2. Registro de clientes
   - Endpoint: POST /register
   - VAC envía id (UUID persistente), hostname, ip y mac.
   - VAS inserta o actualiza el cliente por UUID (sin usar MAC como clave primaria).

3. Regeneración de configuración
   - Si cambia un cliente o aparece uno nuevo, VAS regenera /var/lib/vas/computers.json.
   - VAS incrementa la versión de configuración para que los clientes detecten cambios.

4. Sincronización opcional con Veyon Master
   - Si VEYON_MASTER_SYNC=1 y existe veyon-cli, VAS genera un CSV y actualiza la location gestionada.
   - Se ejecuta en arranque y tras cambios de clientes.
   - La sincronización no bloquea el autoregistro si hay un fallo puntual de Veyon.

5. Publicación de estado
   - Endpoint: GET /version devuelve la versión actual.
   - Endpoint: GET /config devuelve el JSON de configuración para VAC.

## Servicio systemd

- Nombre del servicio: vas.service
- Comandos de operación habituales:
  - sudo systemctl status vas
  - sudo systemctl restart vas
  - sudo journalctl -u vas -f

## Configuracion

Archivo de configuración: etc/vas/vas.conf

Sobreescrituras por subconfiguración: /etc/vas/vas.conf.d/*.conf

El orden de carga es:
1. /etc/vas/vas.conf
2. /etc/vas/vas.conf.d/*.conf (orden lexical)

Variables principales:

- PORT: puerto HTTP del servicio
- DB_PATH: ruta de la base de datos SQLite
- CONFIG_PATH: ruta del computers.json generado
- VERSION_FILE: ruta del fichero de versión de configuración
- VEYON_MASTER_SYNC: habilita sincronización de networkobjects en master (1/0)
- VEYON_LOCATION: location administrada por VAS en Veyon
- VEYON_CSV_PATH: ruta del CSV temporal para importación

Ejemplo típico:

PORT=8000
DB_PATH=/var/lib/vas/vas.db
CONFIG_PATH=/var/lib/vas/computers.json
VERSION_FILE=/var/lib/vas/version
VEYON_MASTER_SYNC=1
VEYON_LOCATION=Autoregistrados
VEYON_CSV_PATH=/var/lib/vas/computers-master.csv

## Construccion del paquete

Desde este directorio:

dpkg-buildpackage -us -uc -b
