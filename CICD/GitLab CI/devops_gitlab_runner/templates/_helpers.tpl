{{/* ---------- 名字 ---------- */}}

{{- define "gitlab-runner.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "gitlab-runner.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* ---------- 标签 ---------- */}}

{{- define "gitlab-runner.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "gitlab-runner.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* 只放「不会随发布变化」的标签，selector 用，绝不能塞 version */}}
{{- define "gitlab-runner.selectorLabels" -}}
app.kubernetes.io/name: {{ include "gitlab-runner.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* ---------- 引用类 ---------- */}}

{{- define "gitlab-runner.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "gitlab-runner.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "gitlab-runner.configSecretName" -}}
{{- if .Values.runner.existingSecret -}}
{{- .Values.runner.existingSecret -}}
{{- else -}}
{{- printf "%s-config" (include "gitlab-runner.fullname" .) -}}
{{- end -}}
{{- end -}}
