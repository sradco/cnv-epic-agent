package metrics

import (
	"github.com/machadovilaca/operator-observability-toolkit/pkg/operatormetrics"
)

var (
	leaderGauge = operatormetrics.NewGauge(
		operatormetrics.MetricOpts{
			Name: "kubevirt_virt_operator_leading_status",
			Help: "Indication for an operating virt-operator.",
		},
	)

	reconcileDuration = operatormetrics.NewHistogram(
		operatormetrics.MetricOpts{
			Name: "kubevirt_virt_controller_reconcile_duration_seconds",
			Help: "Duration of the reconcile loop in seconds.",
		},
	)

	requestTotal = operatormetrics.NewCounterVec(
		operatormetrics.MetricOpts{
			Name: "kubevirt_api_request_total",
			Help: "Total number of API requests.",
		},
		[]string{"method", "code"},
	)
)
