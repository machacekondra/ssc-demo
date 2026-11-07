{{- define "renovate.labels" -}}
app.kubernetes.io/name: renovate
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: ssc-demo
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "renovate.runtimeMounts" -}}
- name: runtime
  mountPath: /work
- name: runtime-tmp
  mountPath: /tmp
{{- end -}}

{{- define "renovate.scmEnv" -}}
- name: RENOVATE_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.gitlab.existingSecret }}
      key: {{ .Values.gitlab.tokenKey }}
- name: RENOVATE_USERNAME
  valueFrom:
    secretKeyRef:
      name: {{ .Values.gitlab.existingSecret }}
      key: {{ .Values.gitlab.usernameKey }}
      optional: true
{{- end -}}

{{- define "renovate.caConfigMap" -}}
{{- if .Values.trustedCA.bundle -}}
renovate-ca
{{- else -}}
{{- .Values.trustedCA.existingConfigMap -}}
{{- end -}}
{{- end -}}
