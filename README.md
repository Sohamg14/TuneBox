# Tunebox: music sharing app on AWS EC2

Users sign up, upload songs, stream them in the browser, like tracks and search the library.

| Part | AWS service / tool | Lab experiment |
|---|---|---|
| Virtual server running the app | EC2 (IaaS) | 4 |
| Song files (private, encrypted) | S3 (Storage as a Service) | 6 |
| Users, songs, likes | SQLite on the instance (can be swapped for RDS) | 7 |
| Firewall, encryption, signed links | Security Group, S3 encryption, presigned URLs | 8 |
| Instance accesses S3 with no keys in code | IAM role + policy | 9 |
| Whole system | Web app on public cloud | 10 |

```
Browser -> Nginx (port 80) -> Gunicorn -> Flask app -> S3 (songs) + SQLite (data)
```

## Setup steps

### 1. S3 bucket
1. S3 -> Create bucket, for example `tunebox-songs-<yourname>` (names are globally unique).
2. Keep **Block all public access** ON.
3. Keep default encryption (SSE-S3) on.

### 2. IAM role for the server
1. IAM -> Policies -> Create policy -> JSON (replace the bucket name):
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
    "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/*"
  }]
}
```
2. Name it `TuneboxS3Access`.
3. IAM -> Roles -> Create role -> AWS service -> EC2 -> attach that policy -> name it `TuneboxEC2Role`.

### 3. Launch the EC2 instance
1. Amazon Linux 2023, t2.micro or t3.micro, new key pair.
2. Security group: SSH (22) from **My IP**, HTTP (80) from anywhere.
3. Advanced details -> **IAM instance profile** -> `TuneboxEC2Role`.

### 4. Copy the project and deploy
On your computer:
```bash
scp -i mykey.pem tunebox.zip ec2-user@<public-ip>:~
ssh -i mykey.pem ec2-user@<public-ip>
```
On the server:
```bash
sudo dnf install -y unzip
unzip tunebox.zip && cd tunebox
./deploy.sh YOUR-BUCKET-NAME ap-south-1
```
Use the region where you created the bucket. Then open `http://<public-ip>`.

### 5. Try it
Sign up, upload an MP3, press play, like it, search for it, and delete it.

## Run on your own computer first (optional)
```bash
pip install -r requirements.txt
python app.py        # no BUCKET_NAME set, so songs are saved in ./uploads
```
Open http://127.0.0.1:5000

## Screenshots for the report
- EC2 instance running and its security group rules
- IAM role and policy
- S3 bucket with uploaded songs and public access blocked
- `sudo systemctl status tunebox`
- The app: sign-up, library, player, upload page

## Troubleshooting
- **Page does not load:** check that port 80 is open in the security group.
- **502 Bad Gateway:** run `sudo journalctl -u tunebox -n 50` to see the app error.
- **"Upload failed":** check the bucket name, region, and that the IAM role is attached to the instance.
- **Song will not play:** the bucket and EC2 must be in the region you passed to `deploy.sh`.
- **Nginx shows its default page:** remove the extra `server` block from `/etc/nginx/nginx.conf`, then `sudo systemctl restart nginx`.
- **Cost:** stop or terminate the instance and empty and delete the bucket when you finish.

## Ideas for extra marks
- Move the database to RDS MySQL (experiment 7)
- HTTPS with a free domain and Let's Encrypt
- CloudWatch monitoring, Auto Scaling and a load balancer
- CloudFront in front of S3 for faster streaming

## Screenshots
Add your images to the `screenshots/` folder and link them here, for example:

```
![Library](screenshots/library.png)
```

## Note on the live demo
The demo was deployed on an EC2 instance and then shut down to avoid AWS charges. Follow the setup steps above to deploy it again.
