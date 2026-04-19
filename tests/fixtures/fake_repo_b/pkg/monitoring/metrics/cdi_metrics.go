package metrics

import (
	"github.com/machadovilaca/operator-observability-toolkit/pkg/operatormetrics"
)

var (
	importProgress = operatormetrics.NewGauge(
		operatormetrics.MetricOpts{
			Name: "kubevirt_cdi_import_progress_total",
			Help: "Progress of current CDI import as a percentage.",
		},
	)

	uploadCount = operatormetrics.NewCounter(
		operatormetrics.MetricOpts{
			Name: "kubevirt_cdi_upload_count_total",
			Help: "Total number of CDI upload operations.",
		},
	)
)
