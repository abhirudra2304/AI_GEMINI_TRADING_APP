import os
import pickle
import time
from collections import defaultdict
import statistics

# This top-level function is pickle-safe and can be used as a factory.
def _nested_int_defaultdict_factory():
    """A pickle-safe factory for creating nested defaultdicts with an int default."""
    return defaultdict(int)

class APIProfiler:
    """A singleton class to track API usage, timings, and other statistics."""

    def __init__(self, report_path="profiler_report.pkl"):
        # Use an absolute path to avoid issues with the current working directory
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.report_path = os.path.join(base_dir, report_path)
        self.reset()

    def reset(self):
        """Resets all statistics, making the profiler ready for a new run."""
        self.start_time = time.monotonic()
        # The lambda factory was causing the pickling error.
        # It's replaced with a pickle-safe, top-level factory function.
        self.stats = defaultdict(_nested_int_defaultdict_factory)
        self.timings = defaultdict(list)
        self.errors = defaultdict(int)
        self.cache_events = defaultdict(int)
        self.requests = []

    def log_request(self, caller, request_type, symbol=None):
        self.stats['requests']['total'] += 1
        self.stats['requests_by_caller'][caller] += 1
        self.stats['requests_by_type'][request_type] += 1
        self.requests.append((time.monotonic(), caller, request_type, symbol))

    def log_timing(self, key, duration):
        self.timings[key].append(duration)

    def log_error(self, error_code):
        self.errors[error_code] += 1

    def log_cache_event(self, event_type):
        self.cache_events[event_type] += 1

    def log_success(self):
        self.stats['requests']['success'] += 1

    def log_retry(self):
        self.stats['requests']['retries'] += 1

    def log_cooldown(self):
        self.stats['requests']['cooldowns'] += 1

    def _convert_defaultdicts_to_dicts(self, d):
        """Recursively converts defaultdicts to dicts for serialization."""
        if isinstance(d, defaultdict):
            return {k: self._convert_defaultdicts_to_dicts(v) for k, v in d.items()}
        return d

    def __getstate__(self):
        """
        Prepare the object for pickling.
        This is called by pickle.dump() and converts defaultdicts to regular dicts,
        making the pickled state independent of the defaultdict factories.
        """
        state = self.__dict__.copy()
        state['stats'] = self._convert_defaultdicts_to_dicts(state.get('stats'))
        state['timings'] = self._convert_defaultdicts_to_dicts(state.get('timings'))
        state['errors'] = self._convert_defaultdicts_to_dicts(state.get('errors'))
        state['cache_events'] = self._convert_defaultdicts_to_dicts(state.get('cache_events'))
        return state

    def __setstate__(self, state):
        """
        Restore the object after unpickling.
        This is called by pickle.load() and restores defaultdict behavior.
        """
        self.__dict__.update(state)

        # Convert dicts back to defaultdicts to restore runtime behavior
        stats_dd = defaultdict(_nested_int_defaultdict_factory)
        if 'stats' in state and isinstance(state['stats'], dict):
            for k, v in state['stats'].items():
                stats_dd[k] = defaultdict(int, v)
        self.stats = stats_dd

        timings_dd = defaultdict(list)
        if 'timings' in state and isinstance(state['timings'], dict):
            timings_dd.update(state['timings'])
        self.timings = timings_dd

        errors_dd = defaultdict(int)
        if 'errors' in state and isinstance(state['errors'], dict):
            errors_dd.update(state['errors'])
        self.errors = errors_dd

        cache_events_dd = defaultdict(int)
        if 'cache_events' in state and isinstance(state['cache_events'], dict):
            cache_events_dd.update(state['cache_events'])
        self.cache_events = cache_events_dd

    def save_report(self):
        """Saves the profiler state to a pickle file."""
        try:
            with open(self.report_path, "wb") as f:
                # __getstate__ will be called automatically to prepare for pickling
                pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as e:
            print(f"🚨 Profiler failed to save report: {e}")

    def load_report(self):
        if os.path.exists(self.report_path):
            try:
                with open(self.report_path, "rb") as f:
                    # Unpickling will use __setstate__ to restore the object
                    loaded_profiler = pickle.load(f)
                    self.__dict__.update(loaded_profiler.__dict__)
                return True
            except Exception as e:
                print(f"⚠️ Could not load profiler report: {e}")
        return False

    def print_report(self, from_cache=False):
        # This method is for display and doesn't need changes for the pickling issue.
        # It's included here for completeness of the class.
        if from_cache:
            print("\n" + "="*80 + "\n📊 PROFILER REPORT (from last run)\n" + "="*80)
        else:
            duration = time.monotonic() - self.start_time
            print("\n" + "="*80 + f"\n📊 PROFILER REPORT (duration: {duration:.2f}s)\n" + "="*80)

        total_requests = self.stats.get('requests', {}).get('total', 0)
        if total_requests > 0:
            print(f"Total API Requests: {total_requests}")
        # Add more detailed printing logic as needed
        print("="*80)

# Singleton instance used across the application
profiler = APIProfiler()