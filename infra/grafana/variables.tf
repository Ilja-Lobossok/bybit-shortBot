variable "grafana_url" {
  description = "Existing Grafana Cloud stack URL, not OTLP gateway or Cloud Portal."
  type        = string
  default     = "https://tanwolf1218.grafana.net"
  validation {
    condition     = can(regex("^https://[A-Za-z0-9-]+\\.grafana\\.net/?$", var.grafana_url))
    error_message = "Use a bare HTTPS Grafana Cloud stack URL without credentials, path or query."
  }
}

variable "metrics_datasource_uid" {
  description = "UID of the existing Cloud Metrics Prometheus datasource; never creates one."
  type        = string
  default     = "grafanacloud-prom"
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,40}$", var.metrics_datasource_uid))
    error_message = "Datasource UID must contain 1-40 ASCII letters, digits, underscores or hyphens."
  }
}

variable "environment" {
  description = "Matches the bot's existing MONITORING_ENVIRONMENT label."
  type        = string
  default     = "production"
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,32}$", var.environment))
    error_message = "Environment must contain 1-32 ASCII letters, digits, underscores or hyphens."
  }
}

variable "alerts_enabled" {
  description = "Keep false for the first approved apply: all rules are paused. Arming needs separate approval."
  type        = bool
  default     = false
}

variable "alert_contact_point_name" {
  description = "Name of an existing notification-only contact point, mandatory when enabling alerts. No trade/Render webhook."
  type        = string
  default     = ""
}
