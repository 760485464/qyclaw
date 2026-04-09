rm -rf certs/
mkdir -p certs/
cd certs/

openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -sha512 -days 3650 -subj "/CN=${DOCKER_NAME}" -key ca.key -out ca.crt
openssl genrsa -out tls.key 4096
openssl req -sha512 -new -subj "/CN=${DOCKER_NAME}" -key tls.key -out tls.csr
cat << EOF > v3.ext
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, nonRepudiation, keyEncipherment, dataEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = @alt_names

[alt_names]
DNS.1=localhost
DNS.2=docker-0.docker
DNS.3=docker-1.docker
DNS.4=docker-0
DNS.5=docker-1
IP.1=127.0.0.1
EOF
openssl x509 -req -sha512 -days 3650 -extfile v3.ext -CA ca.crt -CAkey ca.key -CAcreateserial -in tls.csr -out tls.crt
echo "[INFO] # check docker certificates info"
echo "[CMD] openssl x509 -noout -text -in tls.crt"
openssl x509 -noout -text -in tls.crt
cd ..
