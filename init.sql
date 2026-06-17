CREATE DATABASE IF NOT EXISTS careernexus_db;
USE careernexus_db;
CREATE USER IF NOT EXISTS 'career_app_user'@'%' IDENTIFIED BY 'dbSecur3d';
GRANT SELECT, INSERT, UPDATE, DELETE ON careernexus_db.* TO 'career_app_user'@'%';
FLUSH PRIVILEGES;
CREATE TABLE IF NOT EXISTS jobs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    company VARCHAR(255) NOT NULL,
    link TEXT,
    posted_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS skills (
    id INT AUTO_INCREMENT PRIMARY KEY,
    skill_name VARCHAR(100) UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS job_skills (
    job_id INT,
    skill_id INT,
    PRIMARY KEY (job_id, skill_id),
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE,
    FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS user_resumes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    parsed_data JSON, 
    upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS user_skills (
    user_id INT,
    skill_id INT,
    PRIMARY KEY (user_id, skill_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE
);

-- Normalised view of a parsed resume's work history. The full parsed document
-- is always stored as JSON in user_resumes.parsed_data; these tables make the
-- key sections queryable without unpacking JSON.
CREATE TABLE IF NOT EXISTS resume_experience (
    id INT AUTO_INCREMENT PRIMARY KEY,
    resume_id INT NOT NULL,
    company VARCHAR(255),
    title VARCHAR(255),
    location VARCHAR(255),
    start_date VARCHAR(20),
    end_date VARCHAR(20),
    is_current BOOLEAN DEFAULT FALSE,
    description TEXT,
    FOREIGN KEY (resume_id) REFERENCES user_resumes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resume_education (
    id INT AUTO_INCREMENT PRIMARY KEY,
    resume_id INT NOT NULL,
    institution VARCHAR(255),
    degree VARCHAR(255),
    field_of_study VARCHAR(255),
    start_date VARCHAR(20),
    end_date VARCHAR(20),
    is_current BOOLEAN DEFAULT FALSE,
    gpa VARCHAR(20),
    FOREIGN KEY (resume_id) REFERENCES user_resumes(id) ON DELETE CASCADE
);
