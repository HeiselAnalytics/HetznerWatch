# Security policy

Please do not report security vulnerabilities in a public issue. Contact the
repository owner privately through the security reporting method configured on
the hosting platform.

HetznerWatch stores service credentials in its local SQLite database. They are
not returned by the settings API, but are not encrypted at rest. Restrict access
to the Docker host and data volume, and use an authenticated HTTPS reverse proxy
before exposing the dashboard beyond a trusted local network.
