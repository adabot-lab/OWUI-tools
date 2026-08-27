import time
import uuid


# Setup logging
class Logger:
    def __init__(self):
        self.run_id = str(uuid.uuid4())[:8]
        self.start_time = time.time()
        self.errors = []
        self.warnings = []

    def log(self, message: str, level: str = "INFO"):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_msg = f"[{timestamp}] [{self.run_id}] [{level}] {message}"
        print(log_msg)

        # Capture errors and warnings
        if level == "ERROR":
            self.errors.append(log_msg)
        elif level == "WARNING":
            self.warnings.append(log_msg)

    def print_summary(self):
        """Print a summary of all errors and warnings at the end of the run"""
        total_time = time.time() - self.start_time
        print(f"\n{'='*50}")
        print(f"Run completed in {total_time:.2f} seconds")
        print(f"Run ID: {self.run_id}")
        print(f"{'='*50}")

        if self.errors:
            print(f"\n❌ ERRORS ({len(self.errors)}):")
            for error in self.errors:
                print(f"  {error}")
        else:
            print("\n✅ No errors detected")

        if self.warnings:
            print(f"\n⚠️  WARNINGS ({len(self.warnings)}):")
            for warning in self.warnings:
                print(f"  {warning}")
        else:
            print("\n✅ No warnings detected")

        print(f"{'='*50}")

logger = Logger()
