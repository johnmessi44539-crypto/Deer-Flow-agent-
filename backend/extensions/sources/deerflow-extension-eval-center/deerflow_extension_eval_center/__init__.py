"""An independent, operator-installed DeerFlow evaluation extension."""

from pathlib import Path

from deerflow_extension_api import BackendAction, BrowserAssets, PluginContribution, extension

from .service import EvaluationService


@extension(api="0.2.13", name="eval-center")
def install(registry, config):
    if config.get("enabled", True) is False:
        return
    path = config.get("storage_path")
    if not isinstance(path, str):
        raise ValueError("Configure an absolute storage_path for eval-center")
    timeout = config.get("timeout_seconds", 120)
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise ValueError("timeout_seconds must be an integer in 1..300")
    assistant = config.get("assistant_id", "lead_agent")
    if not isinstance(assistant, str) or not 1 <= len(assistant) <= 100:
        raise ValueError("Invalid assistant_id")
    service = EvaluationService(path, assistant_id=assistant, timeout_seconds=timeout)
    registry.service(service)
    accepted = registry.plugin(
        PluginContribution(
            namespace="local.eval-center",
            title="Agent 评测工作台",
            description="固定用例、受控 Agent 执行、严格 JSON 判分与可复查报告。",
            enabled=True,
            frontend=BrowserAssets("eval-center", Path(__file__).parent),
            backend=tuple(
                BackendAction(name, service.handler(name))
                for name in (
                    "dataset_get",
                    "dataset_list",
                    "dataset_import",
                    "experiment_compare",
                    "experiment_start",
                    "experiment_list",
                    "experiment_get",
                    "experiment_cancel",
                    "report_export",
                )
            ),
        )
    )
    if not accepted:
        raise RuntimeError("This host does not support full-stack plugins")
