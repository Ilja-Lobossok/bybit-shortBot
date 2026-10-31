# GRAFANA_AUTH is read by the provider from the operator's environment.
# Do not put credentials into .tf/.tfvars, resources, outputs or Render.
provider "grafana" {
  url = var.grafana_url
}
