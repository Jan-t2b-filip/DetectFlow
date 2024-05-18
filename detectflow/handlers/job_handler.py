import os
import glob
from detectflow.manipulators.manipulator import Manipulator
from detectflow.manipulators.s3_manipulator import S3Manipulator
from detectflow.validators.s3_validator import S3Validator
from detectflow.manipulators.database_manipulator import DatabaseManipulator
import logging
import sqlite3
import re
from detectflow.handlers.email_handler import EmailHandler
from detectflow.utils.log_file import LogFile
from detectflow.utils.pbs_job_report import PBSJobReport
from detectflow.validators.input_validator import InputValidator
import traceback
from typing import List


class JobHandler:
    def __init__(self,
                 output_directory: str,
                 user_email: str,
                 s3_cfg_file: str = "/storage/brno2/home/chlupp/.s3.cfg",
                 sender_email: str = "detectflow@gmail.com",
                 email_password: str = "mcxxdileklfcleuu",
                 email_handler=None,
                 llm_handler=None):

        self.output_directory = output_directory
        self.s3_manipulator = S3Manipulator(s3_cfg_file)
        self.s3_valdiator = S3Validator(s3_cfg_file)
        self.email_sender = EmailHandler(sender_email, email_password) if email_handler is None else email_handler
        self.llm_handler = llm_handler

        # Validate email address
        if InputValidator.is_valid_email(user_email):
            self.user_email = user_email
        else:
            raise ValueError(f"Invalid email address: {user_email}")

    def handle_finished_job(self, job_info):
        exit_status = job_info.get('exit_status', None)

        if exit_status == 0:
            # Job succeeded, handle accordingly
            print("Job finished successfully.")
            self.handle_successful_job(job_info)
        else:
            # Job failed, handle accordingly
            print("Job failed.")
            self.handle_failed_job(job_info)

    def handle_successful_job(self, job_info):
        job_name = job_info.get('job_name')  # Assuming this is how you track which job it is
        results = {"result_database": False,
                   "training_data": False
                   }

        # Backup database with results
        try:
            data_stats = self.handle_db_file(job_name)
            results["result_database"] = True
        except Exception as e:
            logging.error(f"Error when backing up the result database, perform manual backup: {e}")
            data_stats = {
                "number_of_visits": 0,
                "number_of_frames": 0
            }

        # Check for folders with images and .txt files and back it up
        try:
            image_folder_path = self.find_image_folder(self.output_directory)
            if image_folder_path:
                # shutil.copy(image_folder_path, "/path/to/task_named_folder/")
                self.upload_folder_to_s3(image_folder_path, 'training-data', job_name)
                results["training_data"] = True
        except Exception as e:
            logging.error(f"Error when backing up the generated training data, perform manual backup: {e}")

        # Find a checkpoint file and rename it so it gets ignored when job is rerun but is retained for manual control
        try:
            chp_pattern = rf".*{re.escape(job_name)}.*"
            chp_files = Manipulator.list_files(self.output_directory, regex=chp_pattern, extensions=('.json', '.ini'),
                                               return_full_path=True)
            if len(chp_files) == 1:
                Manipulator.move_file(chp_files[0], self.output_directory,
                                      filename=f"DONE{os.path.splitext(chp_files[0])[-1]}", overwrite=True, copy=False)
            elif len(chp_files) > 1:
                logging.info("More than one checkpoint file found in the output directory. Renaming all.")
                for i, file in enumerate(chp_files):
                    Manipulator.move_file(file, self.output_directory, filename=f"DONE_{i}{os.path.splitext(file)[-1]}",
                                          overwrite=True, copy=False)
            else:
                logging.info("No checkpoint file found in the output directory. Note that this is unexpected.")
        except Exception as e:
            logging.error(
                f"Error when archiving job checkpoint file, it may obstruct job restart. Delete or rename the file before reruning the job: {e}")

        # Compose and send email notification
        try:
            self.send_email_notification(job_info, data_stats, results)
        except Exception as e:
            logging.error(f"Error when sending email notification: {e}")
            traceback.print_exc()
            logging.error(f"Attempting to generate a simple text report...")
            self.print_text_notification(job_info, data_stats, results)

    def handle_failed_job(self, job_info):
        job_name = job_info.get('job_name')  # Assuming this is how you track which job it is
        results = {"result_database": False,
                   "training_data": False
                   }

        try:
            data_stats = self.handle_db_file(job_name)
            results["result_database"] = True
        except Exception as e:
            logging.error(f"Error when backing up the result database, perform manual backup: {e}")
            data_stats = {
                "number_of_visits": 0,
                "number_of_frames": 0
            }

        logging.info(
            "Training data backup was not performed. This is intentional, the data should be backed up when job finishes successfully.")
        logging.info("Checkpoint file retained. It shall be used when restarting the job.")

        # Compose and send email notification
        try:
            self.send_email_notification(job_info, data_stats, results)
        except Exception as e:
            logging.error(f"Error when sending email notification: {e}")
            logging.error(f"Attempting to generate a simple text report...")
            self.print_text_notification(job_info, data_stats, results)

    def send_email_notification(self, job_info, data_stats, results):

        # Extract error log data
        error_logs = self.parse_log_files(job_info)

        # Query LLM
        if self.llm_handler is not None:
            data = {
                "PBS job information returned by the qstat command": job_info,
                "Log files generated by the PBS job": error_logs
            }
            params = {
                'max_tokens': 500,
                'temperature': 0.5
            }
            body = self.llm_handler.compose_job_status_notification(data, **params)
        else:
            body = PBSJobReport(job_info, error_logs).generate_report(format='html')

        # Format appended data
        appendix = EmailHandler.format_data_for_email_as_table({
            "PBS Job Information": job_info,
            "Detection Results": data_stats,
            "Output Logs": error_logs,
            "Results Backed-up": results
        })

        # Gather attachements
        attachments = {"error_log.txt": job_info.get("error_path", None),
                       "output_log.txt": job_info.get("output_path", None),
                       "batch_script.sh": job_info.get("batch_script", None),
                       "python_script.py": job_info.get("python_script", None),
                       "job_config.json": job_info.get("job_config", None)
                       }

        # Send email
        # subject, body = self.email_sender.process_email_text(response)
        subject = f"{job_info.get('status', 'UNKNOWN')} - Job: {job_info.get('job_id', 'Unknown ID')}"
        self.email_sender.send_email(self.user_email, subject, body, appendix, attachments)

    def print_text_notification(self, job_info, data_stats, results):

        try:
            # Extract error log data
            error_logs = self.parse_log_files(job_info)

            # Get body of the report
            body = PBSJobReport(job_info, error_logs).generate_report(format='text')

            # Format appended data
            appendix = self.email_sender.format_data_for_email({
                "PBS Job Information": job_info,
                "Detection Results": data_stats,
                "Output Logs": error_logs,
                "Results Backed-up": results
            })
            # Print the gathered data
            print("Job Status Report:")
            print("")
            print(body)
            print("")
            print(appendix)
        except Exception as e:
            logging.error(f"Failed to generate the fallback text report. Error: {e}")

    def parse_log_files(self, job_info):

        try:
            # Parse error log file
            error_log_file_path = job_info.get("error_path", None)

            if error_log_file_path:
                error_log_file = LogFile(error_log_file_path)
                bash_error_log = error_log_file.formatted_bash_errors
                python_error_log = error_log_file.formatted_python_errors
            else:
                print("No error log information extracted. File not found.")
                bash_error_log, python_error_log = "", ""

            # Parse operation log file
            operation_log_file_path = job_info.get("output_path", None)

            if operation_log_file_path:
                operation_log_file = LogFile(operation_log_file_path)
                operation_log = operation_log_file.formatted_general_logs
            else:
                print("No operation log information extracted. File not found.")
                operation_log = ""
        except Exception as e:
            logging.error(f"Error parsing log files: {e}")
            bash_error_log, python_error_log, operation_log = "", "", ""

        return {"bash_error_log": bash_error_log,
                "python_error_log": python_error_log,
                "operation_log": operation_log}

    def handle_db_file(self, job_name):

        data_stats = None

        # Find the appropriate database file
        db_path = self.find_db_file(job_name)
        if db_path:
            self.upload_db_to_s3(db_path, job_name)

            # Get data stats from db
            data_stats = self.get_data_stats_from_db(db_path)  # no. of visits and frames analyzed

        return data_stats

    def find_db_file(self, job_name):
        """
        Find the latest modified .db file that matches the part of job_name after the first underscore.
        """
        pattern = job_name.split('_', 1)[1]  # Get the part of the job name after the first underscore
        regex = re.compile(re.escape(pattern), re.IGNORECASE)  # Create a regex pattern, ignore case

        db_files = []
        for file in glob.glob(os.path.join(self.output_directory, '*.db')):
            if regex.search(os.path.basename(file)):
                db_files.append(file)

        if not db_files:
            return None

        # Return the file with the latest modification time
        return max(db_files, key=os.path.getmtime)

    def find_image_folder(self, base_path, keywords: List = []):
        # List of keywords to check in directory names
        keywords = keywords + ["frame", "train", "image", "data", "samples"]

        for root, dirs, files in os.walk(base_path):
            # Check if any keyword is in the current root directory's name
            if any(keyword in root for keyword in keywords):
                # Check for the presence of specific file types
                if any(f.endswith('.txt') for f in files) and any(f.endswith(('.png', '.jpg', '.jpeg')) for f in files):
                    return root
        return None

    def upload_db_to_s3(self, local_file_path, job_name):
        try:
            # Specify the bucket and directory names
            bucket_name, directory_name = job_name.split('_', 1)

            # Create bucket
            self.s3_manipulator.create_bucket_s3(bucket_name)

            # Check if the directory exists within the bucket
            self.s3_manipulator.create_directory_s3(bucket_name, directory_name)

            # Upload a file to the specified directory
            s3_file_path = f"{directory_name}{os.path.basename(local_file_path)}"  # Path in the bucket where the file will be uploaded
            self.s3_manipulator.upload_file_s3(bucket_name, local_file_path, s3_file_path)
            print(f"Uploaded {local_file_path} to S3 bucket {bucket_name} successfully.")
        except Exception as e:
            print(f"Failed to upload {local_file_path} to S3: {e}")

    def upload_folder_to_s3(self, local_directory, bucket_name, job_name):
        try:
            # Specify the bucket and directory names
            _, directory_name = job_name.split('_', 1)

            # Ensure the bucket exists
            self.s3_manipulator.create_bucket_s3(bucket_name)

            # Check if the directory exists within the bucket
            self.s3_manipulator.create_directory_s3(bucket_name, directory_name)

            # Upload the directory to S3
            logging.info(f"Uploading directory '{local_directory}' to 's3://{bucket_name}/{directory_name}'")
            self.s3_manipulator.upload_directory_s3(local_directory, bucket_name, directory_name)
        except Exception as e:
            print(f"Failed to upload {local_directory} to S3: {e}")

    def get_data_stats_from_db(self, db_path):
        """
        Retrieve overview data from visits database.
        Data retrieved:
        - visits: count of non-NULL entries in the 'relevant_visitor_bboxes' column from the 'visits' table
        -

        Parameters:
            db_path (str): Path to the SQLite database file.

        Returns:
            dict: A dictionary containing the count of relevant entries.
        """
        try:
            # Connect to the SQLite database
            database_manipulator = DatabaseManipulator(db_path)

            # SQL query to count non-NULL entries
            query = "SELECT COUNT(*) FROM visits WHERE relevant_visitor_bboxes IS NOT 'null' AND relevant_visitor_bboxes IS NOT NULL"
            visits = database_manipulator.fetch_one(query)[
                0]  # Fetch the result which is a tuple, get the first element

            # SQL query to count non-NULL entries
            query = "SELECT COUNT(*) FROM visits"
            frames = database_manipulator.fetch_one(query)[
                0]  # Fetch the result which is a tuple, get the first element

            # Close the database connection
            database_manipulator.close_connection()

            return {"number_of_visits": visits,
                    "number_of_frames": frames
                    }
        except sqlite3.Error as e:
            print(f"Database error: {e}")
            return {"number_of_visits": 0,
                    "number_of_frames": 0
                    }  # Return zero or appropriate error handling