# Sign in with SSO

Hangar can use an OpenID Connect provider for sign-in: Authentik, Microsoft Entra ID, Google, Keycloak, Duo, or any other. It still has one owner; you link your own identity at the provider, and only that identity can sign in.

![Sign-in settings](sso.png)

1. In **Settings > Sign-in**, pick the provider. Copy the **Redirect URI** Hangar shows (`https://<your-hangar>/api/auth/oidc/callback`).
2. At the provider, create an OIDC client (confidential, authorization code) with that redirect URI. Copy its issuer URL, client ID, and client secret into Hangar and click **Save**. Hangar checks `<issuer>/.well-known/openid-configuration` right away.
3. Click **Link with …** and sign in at the provider. Hangar records that identity (issuer + subject).

The sign-in page then shows **Sign in with …** next to the password form. Once that works, you can turn password sign-in off in Settings. If you lose access to the provider, `docker exec -it hangar python -m hangar reset-password` sets a new password and turns password sign-in back on.

The redirect URI is filled in from the address in your browser, and you can edit it. If it shows `localhost`, change it to the address your other devices use, for example `http://192.168.1.10:7878` or `https://hangar.your-tailnet.ts.net`; or set `HANGAR_PUBLIC_URL` in `.env` so Hangar always uses that address. Behind a reverse proxy, Hangar reads `X-Forwarded-Host` and `X-Forwarded-Proto`. Linking has to be done from the redirect address; **Sign in with …** works from any address and moves you there first. Behind HTTPS (the Tailscale sidecar or a reverse proxy), also set `HANGAR_SECURE_COOKIES=true`.

[Back to the README](../README.md)
