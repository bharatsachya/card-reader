#!/usr/bin/env bash
#
# Issue a Let's Encrypt certificate and serve HTTPS.
#
#   ./deploy/enable-tls.sh
#
# WHY sslip.io. A public CA will not issue a certificate for a bare IP address.
# sslip.io resolves <anything>.<ip>.sslip.io back to that IP, so
# 51.20.232.225.sslip.io is a real hostname pointing here, with no domain to
# buy. The trade-off is a third-party DNS dependency: if sslip.io is down the
# name stops resolving, though the IP keeps working.
#
# WHY THIS CAN RUN BEFORE PORT 443 IS OPEN. Let's Encrypt's HTTP-01 challenge
# is served over port 80, which is already reachable. So the certificate can be
# issued and nginx configured now; HTTPS simply starts working the moment the
# security group allows 443.

set -euo pipefail
DOMAIN="${1:-51.20.232.225.sslip.io}"
EMAIL="${2:-teambytebell@gmail.com}"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

log "Checking the name resolves to this machine"
RESOLVED=$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1)
MYIP=$(curl -s --max-time 10 https://checkip.amazonaws.com || echo "")
echo "  $DOMAIN -> ${RESOLVED:-nothing}"
echo "  this host  -> ${MYIP:-unknown}"
if [ -n "$RESOLVED" ] && [ -n "$MYIP" ] && [ "$RESOLVED" != "$MYIP" ]; then
    echo "  name does not point here; certbot would fail the challenge" >&2
    exit 1
fi

log "Installing certbot"
sudo apt-get update -qq
sudo apt-get install -y -qq certbot python3-certbot-nginx >/dev/null

log "Issuing the certificate"
# --no-redirect is deliberate and important. certbot's default is to add an
# HTTP -> HTTPS redirect, which would immediately break the site: port 443 is
# not open yet, so every visitor would be redirected to a port that drops
# their packets. The redirect is enabled separately, once 443 is confirmed
# reachable, by rerunning with REDIRECT=1.
sudo certbot --nginx \
    -d "$DOMAIN" \
    --non-interactive --agree-tos --email "$EMAIL" \
    --no-redirect \
    --keep-until-expiring

log "Result"
sudo nginx -t
sudo systemctl reload nginx
echo "  listening on: $(ss -ltn | awk '/:443|:80 /{print $4}' | tr '\n' ' ')"
sudo certbot certificates 2>/dev/null | grep -E "Certificate Name|Domains|Expiry" | sed 's/^/  /'

if [ "${REDIRECT:-0}" = "1" ]; then
    log "Enabling the HTTP -> HTTPS redirect"
    sudo certbot --nginx -d "$DOMAIN" --non-interactive --redirect --keep-until-expiring
    sudo systemctl reload nginx
    echo "  http:// now redirects to https://"
fi

echo
echo "Renewal is handled by the certbot systemd timer:"
systemctl list-timers 'certbot*' --no-pager 2>/dev/null | head -3 | sed 's/^/  /'
