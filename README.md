# Boletín semanal (ECDC) – Agente automático

Envía por correo un **resumen en español** del último informe semanal del ECDC.

## 1) Secrets necesarios (Settings → Secrets and variables → Actions)
Crea **uno por uno**:
- `SMTP_SERVER` → `smtp.gmail.com`
- `SMTP_PORT` → `465`
- `SENDER_EMAIL` → `agentia70@gmail.com`
- `RECEIVER_EMAIL` → `contra1270@gmail.com`
- `EMAIL_PASSWORD` → **Contraseña de aplicación** de `agentia70@gmail.com` (16 caracteres, 2FA activada)

## 2) Ejecutar
- Pestaña **Actions** → workflow **Enviar resumen semanal del ECDC** → **Run workflow**
- **Modo prueba**: establece `DRY_RUN=1` en los secrets para no enviar correo

## 3) Funcionamiento
El agente:
1. Busca el PDF más reciente del ECDC
2. Extrae el texto
3. Genera un resumen automático
4. Traduce al español
5. Envía por email el resultado

## 📧 Configuración de Gmail
Para Gmail, necesitas:
1. Activar verificación en 2 pasos
2. Generar una "contraseña de aplicación" de 16 caracteres
3. Usar esa contraseña en `EMAIL_PASSWORD`
