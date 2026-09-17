/**
 * api/jobHistory.js — Local storage persistence for in-progress job tracking.
 *
 * Since there's no GET /api/queries (list) endpoint on the backend,
 * we track submitted jobs in localStorage so QueryHistory can show
 * in-progress jobs. Completed jobs are fetched from GET /api/reports.
 */

const STORAGE_KEY = 're-search-jobs';

/**
 * @typedef {object} StoredJob
 * @property {string} jobId
 * @property {string} queryText
 * @property {string} status - 'pending' | 'running' | 'done' | 'error'
 * @property {string[]} sources
 * @property {string} createdAt - ISO-8601
 * @property {string|null} reportId - set when a report is available
 */

function _load() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function _save(jobs) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(jobs));
}

/**
 * Add a newly submitted job to history.
 */
export function addJob(jobId, queryText, sources = []) {
  const jobs = _load();    // Loads existing array of jobs from browser's Local Storage.
  //Avoid duplicates
  if (jobs.some((j) => j.jobId === jobId)) return;

  jobs.unshift({ // Adds Job to beginning (unshift) of the jobs array
    jobId,
    queryText,
    status: 'pending',  // set starting status as pending
    sources,
    createdAt: new Date().toISOString(),
    reportId: null,
  });

  // Keep max 50 entries
  if (jobs.length > 50) jobs.length = 50;    // chop off last jobs to limit total jobs to 50 only

  _save(jobs); // saves it back into the browser's Local Storage
}

/**
 * Update a job's status and optionally its reportId.
 */
export function updateJob(jobId, updates) {
  const jobs = _load();
  const idx = jobs.findIndex((j) => j.jobId === jobId);
  if (idx !== -1) {
    jobs[idx] = { ...jobs[idx], ...updates };
    _save(jobs);
  }
}

/**
 * Get all stored jobs.
 * @returns {StoredJob[]}
 */
export function getJobs() {
  return _load();
}

/**
 * Remove a job from history.
 */
export function removeJob(jobId) {
  const jobs = _load().filter((j) => j.jobId !== jobId);
  _save(jobs);
}
