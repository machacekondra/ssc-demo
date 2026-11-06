{{- define "renovate.labels" -}}
app.kubernetes.io/name: renovate
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: ssc-demo
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "renovate.caConfigMap" -}}
{{- if .Values.trustedCA.bundle -}}
renovate-ca
{{- else -}}
{{- .Values.trustedCA.existingConfigMap -}}
{{- end -}}
{{- end -}}
