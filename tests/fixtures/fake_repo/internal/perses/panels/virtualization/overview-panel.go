package virtualization

import (
	"github.com/perses/perses/go-sdk/panel"
	"github.com/perses/perses/go-sdk/panel/panelgroup"
	"github.com/perses/perses/go-sdk/query"
)

func OverviewTotalClusters(datasourceName string) panelgroup.Option {
	return panelgroup.AddPanel("Total Clusters",
		panel.AddQuery(query.PromQL(
			overviewTotalClusters,
		)),
	)
}

func OverviewTotalVMs(datasourceName string) panelgroup.Option {
	return panelgroup.AddPanel("Total Virtual Machines",
		panel.AddQuery(query.PromQL(
			overviewTotalVMs,
		)),
	)
}

func OverviewCPUUsage(datasourceName string) panelgroup.Option {
	return panelgroup.AddPanel("CPU Usage",
		panel.AddQuery(query.PromQL(
			overviewCPUUsage,
		)),
	)
}
