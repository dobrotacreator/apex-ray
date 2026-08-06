from apex_ray.models import AnalyzerResult


def analyzer_warning_labels(result: AnalyzerResult) -> list[str]:
    summaries = {summary.message: summary for summary in result.warning_summaries}
    labels: list[str] = []
    for warning in result.warnings:
        summary = summaries.get(warning)
        if summary is None or summary.occurrences <= 1:
            labels.append(warning)
            continue
        shard_label = ""
        if summary.shard_indexes:
            shard_label = "; shards " + ", ".join(str(index) for index in summary.shard_indexes)
        labels.append(f"{warning} (repeated {summary.occurrences} times{shard_label})")
    return labels
