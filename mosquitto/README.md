# Mosquitto MQTT Broker — Auth Configuration

## File manifest

| File | Purpose | Permissions |
|------|---------|-------------|
| `mosquitto-auth.conf` | mosquitto 2.x `.conf` syntax (NOT YAML — `listeners:` blocks in YAML crash the broker per memory `mosquitto-2-config-format`) | 644 |
| `passwords` | argon2id-hashed credentials (1 user: `monitorung`) | 600, owner `mosquitto:mosquitto` (uid 1883) |

## Generate the initial password file

```bash
# 1. Generate a strong random password (32 hex chars)
MQTT_PASSWORD=$(openssl rand -hex 16)
echo "Generated: $MQTT_PASSWORD"

# 2. Write the hashed password file as the local user
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$(pwd)/server/mosquitto:/out" eclipse-mosquitto:2 \
  sh -c "mosquitto_passwd -c -b /out/passwords monitorung \"$MQTT_PASSWORD\" && chmod 600 /out/passwords"

# 3. Update .env.production:
#    MQTT_USERNAME=monitorung
#    MQTT_PASSWORD=$MQTT_PASSWORD

# 4. Update Android build.gradle.kts default OR pass via:
#    ./gradlew assembleProdRelease -PmqttUsername=monitorung -PmqttPassword=$MQTT_PASSWORD

# 5. Restart services:
docker compose --env-file .env.production up -d --force-recreate --no-deps mqtt subscriber
# (api container doesn't connect to MQTT directly, but rebuild api image anyway
#  so new config.py + mqtt_subscriber.py code is included)
docker compose --env-file .env.production build --no-cache api
docker compose --env-file .env.production up -d --force-recreate --no-deps api web
```

## Rotation procedure (every 90 days recommended)

```bash
# 1. Generate new password
NEW_PASS=$(openssl rand -hex 16)

# 2. Update mosquitto password file (preserves existing users; just updates monitorung's password)
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$(pwd)/server/mosquitto:/out" eclipse-mosquitto:2 \
  sh -c "mosquitto_passwd -b /out/passwords monitorung \"$NEW_PASS\" && chmod 600 /out/passwords"

# 3. Update .env.production with new MQTT_PASSWORD

# 4. Force-restart mqtt to reload the password file
docker compose --env-file .env.production up -d --force-recreate mqtt

# 5. Rebuild + redeploy subscriber with new env
docker compose --env-file .env.production build --no-cache api
docker compose --env-file .env.production up -d --force-recreate subscriber

# 6. Rebuild Android APK with new gradle property, push via Play Console or sideload
./gradlew assembleProdRelease -PmqttPassword=$NEW_PASS
```

## Verification

```bash
# From inside the broker network:
mosquitto_pub -h mqtt -p 1883 -t test/topic -m 'no-auth' 2>&1
#   → Connection error: not authorised (correct)

mosquitto_pub -h mqtt -p 1883 -u monitorung -P wrongpass -t test/topic -m 'wrong' 2>&1
#   → Connection error: not authorised (correct)

mosquitto_pub -h mqtt -p 1883 -u monitorung -P "$MQTT_PASSWORD" -t test/topic -m 'auth-ok'
#   → silent (correct)
```

## Security trade-offs

| Concern | Mitigation |
|---------|------------|
| APK contains the MQTT password (extractable from decompiled APK) | Same shared secret model used by most MQTT IoT apps. For higher security, future wave: TLS + client certificates per device. |
| Same password for all Android installs | Devices are identical roles (publisher + subscriber). Per-device cert would scale poorly for the school-monitoring use case. |
| Subscriber container has full broker access (can publish + subscribe to everything) | Trust boundary: subscriber runs in trusted Docker network. If you want to restrict, add ACLs in `mosquitto-auth.conf` with `acl_file /mosquitto/acl`. |

## Why not TLS?

TLS (mqtts://) on port 8883 with self-signed or Let's Encrypt cert would prevent sniffing AND enable cert-pinning on the client. Trade-off: cert rotation every 90 days, no benefit over password + Cloudflare Tunnel (which already provides TLS termination for the public leg). Defer to a future wave if the internal-network eavesdropping threat model becomes relevant.

## See also

- `server/CLAUDE.md` "Audit Round — Wave 4 (2026-10-05): MQTT broker auth" for the audit rationale
- `server/mosquitto/passwords` — actual hashed credential file (gitignored — contains the hash, not the password itself)
- Docker compose service `mqtt` in `/docker-compose.yml` for the deployment config