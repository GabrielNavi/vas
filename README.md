# vx-dga-l-vas

Paquete Debian para Vitalinux que instala Vitalinux Autoregistration Server (VAS) de forma global.

## Descripcion

Este paquete instala VAS en el sistema para su despliegue mediante Migasfree en entornos Vitalinux. VAS expone una API HTTP basada en FastAPI, almacena clientes en SQLite y genera automáticamente el archivo computers.json de Veyon para que los clientes VAC puedan sincronizar la configuración.

## Requisitos

- Sistema Vitalinux compatible con paquetes Debian.
- Python 3 en tiempo de ejecución.
- Dependencias Python de sistema:
  - python3-fastapi
  - python3-uvicorn
  - python3-pydantic
- systemd para gestión del servicio.

## Informacion del Paquete

- Nombre: vx-dga-l-vas
- Version: 0.3-2
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

4. Publicación de estado
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

Variables principales:

- PORT: puerto HTTP del servicio
- DB_PATH: ruta de la base de datos SQLite
- CONFIG_PATH: ruta del computers.json generado

Ejemplo típico:

PORT=8000
DB_PATH=/var/lib/vas/vas.db
CONFIG_PATH=/var/lib/vas/computers.json

## Construccion del paquete

Desde este directorio:

dpkg-buildpackage -us -uc -b
