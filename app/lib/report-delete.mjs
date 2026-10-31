export function retainReportSelection(ids, jobs) {
  const live = new Set(jobs.map((job) => job.id));
  return [...new Set(ids)].filter((id) => live.has(id));
}

// Keep requests bounded and report each outcome; one failed deletion must not
// stop the remaining selected reports from being cleaned up.
export async function deleteSelectedReports(jobs, remove, onResult = () => {}) {
  const targets = [...new Map(jobs.map((job) => [job.id, job])).values()];
  const results = new Array(targets.length);
  let next = 0,
    done = 0;
  async function worker() {
    while (next < targets.length) {
      const index = next++,
        job = targets[index];
      let result;
      try {
        const response = await remove(job);
        result = { ...job, ok: true, pending: response?.node_cleanup_pending === true };
      } catch (error) {
        result =
          error.status === 404
            ? { ...job, ok: true, pending: false, alreadyDeleted: true }
            : { ...job, ok: false, error: error.message || '删除未完成，请重试' };
      }
      results[index] = result;
      onResult(result, ++done, targets.length);
    }
  }
  await Promise.all(Array.from({ length: Math.min(2, targets.length) }, worker));
  return results;
}
