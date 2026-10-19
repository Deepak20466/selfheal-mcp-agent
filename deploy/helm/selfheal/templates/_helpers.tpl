{{- define "selfheal.fullname" -}}
selfheal
{{- end -}}

{{- define "selfheal.postgresHost" -}}
selfheal-postgres
{{- end -}}

{{- define "selfheal.databaseUrl" -}}
postgresql+asyncpg://{{ .Values.postgres.username }}:{{ .Values.postgres.password }}@{{ include "selfheal.postgresHost" . }}:5432/{{ .Values.postgres.database }}
{{- end -}}

{{- define "selfheal.targetAppDatabaseUrl" -}}
postgresql+asyncpg://{{ .Values.postgres.targetAppUsername }}:{{ .Values.postgres.targetAppPassword }}@{{ include "selfheal.postgresHost" . }}:5432/{{ .Values.postgres.targetAppDatabase }}
{{- end -}}

{{- define "selfheal.image" -}}
{{- $repo := index .Values.image.repository .pod -}}
{{- if .Values.image.registry -}}
{{ .Values.image.registry }}/{{ $repo }}:{{ .Values.image.tag }}
{{- else -}}
{{ $repo }}:{{ .Values.image.tag }}
{{- end -}}
{{- end -}}

{{- define "selfheal.commonEnv" -}}
- name: ENVIRONMENT
  value: {{ .Values.env.environment | quote }}
- name: LOG_LEVEL
  value: {{ .Values.env.logLevel | quote }}
- name: APP_PORT
  value: {{ .Values.app.port | quote }}
- name: SENTINEL_PORT
  value: {{ .Values.sentinel.port | quote }}
- name: MCP_PORT
  value: {{ .Values.mcp.port | quote }}
- name: HEALER_PORT
  value: {{ .Values.healer.port | quote }}
- name: MCP_HOST
  value: "0.0.0.0"
- name: MCP_CLIENT_URL
  value: "http://selfheal-mcp:{{ .Values.mcp.port }}/mcp"
- name: SENTINEL_INGEST_URL
  # core/config.py:sentinel_base_url defaults to http://localhost:{port} --
  # a same-host-deployment assumption. app-pod is a separate Pod here, so
  # this override is required for the smoke test's /trigger/zero call to
  # actually reach sentinel-pod's real /ingest/error endpoint.
  value: "http://selfheal-sentinel:{{ .Values.sentinel.port }}"
- name: HEALER_WEBHOOK_URL
  value: "http://selfheal-sentinel:{{ .Values.sentinel.port }}/webhooks/ci"
- name: PUBLIC_URL
  value: "http://selfheal-sentinel:{{ .Values.sentinel.port }}"
- name: GITHUB_PROTECTED_BRANCHES
  value: {{ .Values.security.protectedBranches | quote }}
- name: GITHUB_ALLOWED_PUSH_BRANCHES
  value: {{ .Values.security.allowedPushBranches | quote }}
{{- end -}}

{{- define "selfheal.databaseEnv" -}}
- name: DATABASE_URL
  value: {{ include "selfheal.databaseUrl" . | quote }}
{{- end -}}

{{- define "selfheal.githubEnv" -}}
{{- if ne .Values.env.environment "production" }}
- name: GITHUB_TOKEN
  valueFrom:
    secretKeyRef:
      name: selfheal-secrets
      key: github-token
{{- end }}
- name: GITHUB_REPO
  value: {{ .Values.env.githubRepo | quote }}
- name: GITHUB_ALLOWED_REPOSITORIES
  value: {{ .Values.env.githubAllowedRepositories | quote }}
- name: GITHUB_APP_ID
  value: {{ .Values.env.githubAppId | quote }}
- name: GITHUB_APP_INSTALLATION_ID
  value: {{ .Values.env.githubAppInstallationId | quote }}
- name: GITHUB_APP_PRIVATE_KEY
  valueFrom:
    secretKeyRef:
      name: selfheal-secrets
      key: github-app-private-key
{{- end -}}

{{- define "selfheal.sentinelTokenEnv" -}}
- name: SENTINEL_INGEST_TOKEN
  valueFrom:
    secretKeyRef:
      name: selfheal-secrets
      key: sentinel-ingest-token
{{- end -}}

{{- define "selfheal.sentinelWebhookEnv" -}}
- name: HEALER_WEBHOOK_SECRET
  valueFrom:
    secretKeyRef:
      name: selfheal-secrets
      key: healer-webhook-secret
{{- end -}}

{{- define "selfheal.healerEnv" -}}
- name: SESSION_SECRET
  valueFrom:
    secretKeyRef:
      name: selfheal-secrets
      key: session-secret
- name: ADMIN_PASSWORD_HASH
  valueFrom:
    secretKeyRef:
      name: selfheal-secrets
      key: admin-password-hash
- name: AI_BACKEND
  value: {{ .Values.env.aiBackend | quote }}
- name: ANTHROPIC_API_KEY
  value: {{ .Values.env.anthropicApiKey | quote }}
- name: ANTHROPIC_BASE_URL
  value: {{ .Values.env.anthropicBaseUrl | quote }}
{{- end -}}

{{- define "selfheal.egressProxyEnv" -}}
- name: HTTPS_PROXY
  value: "http://selfheal-egress:{{ .Values.egress.port }}"
- name: HTTP_PROXY
  value: "http://selfheal-egress:{{ .Values.egress.port }}"
- name: NO_PROXY
  value: "localhost,127.0.0.1,::1,selfheal-app,selfheal-sentinel,selfheal-mcp,selfheal-healer,selfheal-postgres,selfheal-egress,.svc,.svc.cluster.local"
- name: https_proxy
  value: "http://selfheal-egress:{{ .Values.egress.port }}"
- name: http_proxy
  value: "http://selfheal-egress:{{ .Values.egress.port }}"
- name: no_proxy
  value: "localhost,127.0.0.1,::1,selfheal-app,selfheal-sentinel,selfheal-mcp,selfheal-healer,selfheal-postgres,selfheal-egress,.svc,.svc.cluster.local"
{{- end -}}

{{- define "selfheal.dnsPodSpec" -}}
dnsPolicy: None
dnsConfig:
  nameservers:
    - {{ required "dns.filterServiceIP must be reserved for the Selfheal DNS filter Service" .Values.dns.filterServiceIP | quote }}
  searches:
    - {{ .Release.Namespace }}.svc.cluster.local
    - svc.cluster.local
    - cluster.local
  options:
    - name: ndots
      value: "5"
{{- end -}}
