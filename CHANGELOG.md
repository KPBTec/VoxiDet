# CHANGELOG

Historial de versiones de **VoxiDet**, el servicio de detección de contestador automático (AMD) con IA para marcadores Vicidial/Asterisk. Este changelog resume los cambios desde la perspectiva de quien opera la plataforma (nuevas funciones, mejoras y correcciones), sin detalles internos de implementación.

Todas las versiones siguen el esquema `MAJOR.MINOR.PATCH`:
- **MAJOR**: cambios de arquitectura o cambios importantes de compatibilidad.
- **MINOR**: nuevo módulo o mejora significativa.
- **PATCH**: corrección de errores, ajustes de interfaz, mejoras menores.

---

## v1.22.0 — 2026-08-03

- Nuevo: página de **Dashboard** al entrar al panel, con la salud del servidor (CPU/RAM/disco) y un resumen de las detecciones de hoy (llamadas, HUMAN/VOICEMAIL/UNKNOWN, clientes activos).
- Nuevo: **Sistema → Logs backend**, para ver los logs técnicos del servidor directamente desde el panel, sin necesidad de acceso por consola.
- Nuevo: el dominio/URL pública del servidor ahora se puede configurar desde **Sistema**, sin editar archivos de configuración a mano.
- Mejorado: el menú lateral ahora se organiza en acordeón (un grupo a la vez) y el logo de arriba es un acceso directo al Dashboard.

---

## v1.21.4 — 2026-08-03

- Corregido un error crítico introducido en la actualización anterior que hacía fallar por completo el panel administrativo (error 500 en todas las páginas). Recomendamos actualizar cuanto antes si venías de la v1.21.3 o v1.19.3-v1.21.2.

---

## v1.21.3 — 2026-08-03

- Mejoras internas de rendimiento y confiabilidad del panel, sin cambios visibles.

---

## v1.21.2 — 2026-08-03

- Corregido: el ítem "Firewall" del menú lateral no se resaltaba al estar en esa página.

---

## v1.21.1 — 2026-08-03

- Mejoras internas de mantenimiento del panel, sin cambios visibles.

---

## v1.21.0 — 2026-08-03

- Nuevo diseño visual del panel administrativo: paleta bronce/grafito y tipografía renovada, alineado con el resto de las plataformas KPBTec.
- Nuevo selector de tema: elegí entre Bronce (default), Papel, Fósforo o Vidrio desde el pie del menú lateral.

---

## v1.20.0 — 2026-08-03

- Mejoras internas en el motor de actualizaciones de la base de datos, sin cambios visibles.

---

## v1.19.3 — 2026-08-03

- Mejoras de seguridad del panel y del instalador: el despliegue ahora detecta con más precisión si algún componente (base de datos, caché o la API) no llegó a levantar correctamente.

---

## v1.19.2 — 2026-07-22

- El instalador ahora pregunta directamente si querés instalar Whisper Large (más preciso, más lento) en vez de tener que editar un archivo de configuración a mano.
- Corregido: el nombre del modelo activo se cortaba en dos líneas en Proveedores cuando era largo.

---

## v1.19.1 — 2026-07-22

- Corregido un problema importante: en modo Streaming, las keys y el modelo configurados desde el panel (Groq, Deepgram, OpenAI, Together, Fireworks) no se estaban aplicando — solo el modo Batch los respetaba. Ambos modos ya usan la misma configuración.

---

## v1.19.0 — 2026-07-22

- Nuevo proveedor local opcional: Whisper large-v3 completo (más preciso que la versión turbo ya existente, considerablemente más lento). Debe activarse manualmente por cliente — nunca se usa como respaldo automático de otro proveedor.

---

## v1.18.0 — 2026-07-21

- El instalador ahora muestra la versión instalada y la del repositorio, y pide confirmación antes de actualizar (mismo comportamiento que VoxiKam).
- Corregido: el proveedor local "Sherpa-onnx" nunca se podía activar — faltaban tres piezas en la instalación, ahora resueltas.

---

## v1.17.0 — 2026-07-21

- Nueva página propia para gestionar las API Keys de los proveedores de transcripción — antes vivían apretadas dentro de un cuadro de edición chico.
- Menú lateral reorganizado en tres secciones (Operación, Detección, Sistema) en vez de una lista larga sin agrupar.

---

## v1.16.1 — 2026-07-21

- Ahora se pueden desactivar (aunque no eliminar) las API Keys configuradas por archivo, directamente desde el panel — antes solo era posible con las agregadas desde ahí.

---

## v1.16.0 — 2026-07-21

- Nuevo: cargá y gestioná las API Keys de los proveedores de transcripción (Groq, Deepgram, OpenAI, Together, Fireworks) directamente desde el panel, sin editar archivos de configuración. Los valores se guardan cifrados y nunca se vuelven a mostrar en texto plano.

---

## v1.15.1 — 2026-07-20

- Corregido: bajo alto volumen de llamadas simultáneas, la rotación de keys de Groq podía generar más errores de límite de los necesarios.

---

## v1.15.0 — 2026-07-07

- Nuevo en Reportes: detalle por día y cliente en la vista mensual, y un ranking de las transcripciones de buzón de voz más repetidas (útil para detectar mensajes grabados recurrentes).

---

## v1.14.1 — 2026-07-07

- Corregido: con muchos agentes conectados simultáneamente, algunas llamadas atendidas por una persona se marcaban erróneamente como buzón de voz por un corte prematuro de la grabación.

---

## v1.14.0 — 2026-07-07

- Los motores de transcripción locales y gratuitos (Vosk/Sherpa) ahora también están disponibles como respaldo en modo Batch — antes solo se usaban en modo Streaming.
- Corregida una clasificación incorrecta que marcaba como buzón de voz a personas que atendían y hablaban de corrido sin la pausa habitual.

---

## v1.13.4 — 2026-07-06

- Corregido: Deepgram, OpenAI, Together y Fireworks tampoco estaban usando el modelo configurado en el panel ni rotando entre las keys disponibles (mismo problema ya corregido para Groq en la versión anterior).

---

## v1.13.3 — 2026-07-06

- Corregido: cambiar el modelo de Groq desde Proveedores no tenía efecto real, y el sistema no rotaba entre las keys configuradas — esto podía saturar la primera key bajo volumen alto de llamadas y derivar de más a otro proveedor.

---

## v1.13.2 — 2026-07-06

- Mejoras internas de mantenimiento del código, sin cambios visibles.

---

## v1.13.1 — 2026-07-06

- Corregido un consumo de memoria que crecía lentamente con el tiempo en el limitador de tráfico interno del servidor.

---

## v1.13.0 — 2026-07-04

- Nuevo: elegí por cliente cómo se comporta la detección ante casos ambiguos. Por defecto asume buzón de voz ante la duda (comportamiento histórico); el modo alternativo devuelve "desconocido" en su lugar — requiere ajustar el dialplan de Asterisk del cliente para aprovecharlo.

---

## v1.12.6 — 2026-07-04

- Nueva página pública de presentación de VoxiDet.

---

## v1.12.5 — 2026-07-02

- El instalador ahora muestra un resumen con comandos de diagnóstico si algún componente no llega a responder al final del despliegue, en vez de cortar con un error genérico.

---

## v1.12.4 — 2026-07-02

- Mejoras internas de instalación (mismo mecanismo que usa VoxiKam), sin cambios visibles.

---

## v1.12.3 — 2026-07-02

- Mejoras internas de instalación, sin cambios visibles.

---

## v1.12.2 — 2026-07-02

- Corregido un problema que hacía fallar aproximadamente la mitad de las conexiones en modo Streaming.

---

## v1.12.1 — 2026-07-02

- Agregado pie de página con la marca KPBTec en todas las páginas del panel.
- Corregida una superposición visual entre el número de versión y otros elementos de la pantalla.

---

## v1.12.0 — 2026-07-02

- Nueva columna "Modo" (Batch/Stream) en Logs en vivo.

---

## v1.11.6 — 2026-07-01

- Corregido un error que impedía la actualización automática del script instalado en los servidores Asterisk.

---

## v1.11.5 — 2026-07-01

- Corregidos dos problemas importantes: el modo Streaming fallaba siempre y caía en silencio a modo Batch; y el modo Batch ignoraba el proveedor de transcripción configurado por cliente, usando siempre Deepgram sin importar la configuración real.

---

## v1.11.4 — 2026-07-01

- Corregido un problema de configuración que podía dejar sin funcionar la detección en los servidores Asterisk si la URL pública del servidor no incluía el esquema (`http://`).

---

## v1.11.3 — 2026-07-01

- Corregido un problema de compatibilidad que impedía que la detección funcionara en servidores Asterisk con versiones de Python más antiguas.

---

## v1.11.2 — 2026-07-01

- Corregido un problema crítico: la actualización automática del script en los servidores Asterisk podía dejar la detección completamente inactiva en todas las llamadas.

---

## v1.11.1 — 2026-07-01

- Corregido: instalaciones existentes podían perder la conexión a la base de datos al actualizar desde la versión anterior.

---

## v1.11.0 — 2026-07-01

- Mejoras internas de instalación (mismo mecanismo que usa VoxiKam), sin cambios visibles.

---

## v1.10.0 — 2026-07-01

- El instalador ahora pregunta los datos necesarios en la primera instalación (usuario administrador, URL pública, etc.) y genera automáticamente el resto de las claves internas.

---

## v1.9.0 — 2026-07-01

- El ajuste automático de recursos del servidor (memoria, procesos) ahora también se aplica en cada reinicio, no solo al reinstalar.

---

## v1.8.3 — 2026-07-01

- Corregido un error al filtrar Reportes y Detecciones por "todos los clientes".

---

## v1.8.2 — 2026-07-01

- El tono de contestador detectado ahora se guarda y puede verse en Logs en vivo (columna "Beep").

---

## v1.8.1 — 2026-07-01

- Corregido: el selector de motor de detección de voz (Silero) en Proveedores no tenía efecto real sobre las llamadas.

---

## v1.8.0 — 2026-07-01

- Nueva detección experimental del tono de buzón de voz (por ahora solo se registra, todavía no participa en la decisión HUMAN/VOICEMAIL).

---

## v1.7.4 — 2026-07-01

- Mejora de rendimiento en la autenticación bajo alto volumen de llamadas simultáneas.

---

## v1.7.3 — 2026-07-01

- Mejora de rendimiento interno bajo alto volumen de llamadas, sin cambios visibles.

---

## v1.7.2 — 2026-07-01

- Mejorada la velocidad de búsqueda por número de teléfono en Logs en vivo, y agregado filtro por día.

---

## v1.7.1 — 2026-07-01

- Mejora de rendimiento bajo alto volumen de llamadas simultáneas (contadores diarios de uso).

---

## v1.7.0 — 2026-07-01

- Mejora de rendimiento del servidor: mayor capacidad de procesamiento simultáneo de llamadas sin necesidad de más memoria.

---

## v1.6.1 — 2026-07-01

- Mejora de rendimiento bajo alto volumen de llamadas hacia los proveedores de transcripción externos.

---

## v1.6.0 — 2026-07-01

- El instalador ahora ajusta automáticamente los recursos del servidor (memoria, cantidad de procesos) según el tamaño real de la máquina, en vez de usar valores fijos.

---

## v1.5.1 — 2026-07-01

- Nuevo: ver y desbanear IPs bloqueadas por seguridad directamente desde Firewall (puede demorar hasta 1 minuto en reflejarse).

---

## v1.5.0 — 2026-07-01

- Nuevo: protección contra fuerza bruta (bloqueo automático de IPs) para SSH e intentos de acceso inválidos a la API o al panel.
- Nueva página de inicio pública y logo propio de VoxiDet.
- Agregada la licencia del proyecto.
- Corregido un error crítico que impedía que la API arrancara correctamente.
- Firewall: los badges de Acción/Servicio/Estado ahora son visualmente consistentes con el resto del panel.

---

## v1.2.0 — 2026-06-30

- Renombrado de "AMD Server" a **VoxiDet**, con ícono y diseño de panel renovados.

---

## v1.1.0 — 2026-06-27

- Nuevos proveedores de transcripción: OpenAI, Together AI y soporte ampliado de Fireworks.
- Varias correcciones de estabilidad en Estadísticas y Logs en vivo.

---

## v1.0.0 — 2026-06-22

- Primera versión de VoxiDet: instalador de un solo comando, panel de administración (clientes, logs en vivo, keywords, proveedores, consumo), detección en dos capas (análisis de audio + transcripción con IA), y AGI para integración directa con Asterisk.
