CC      = gcc
CFLAGS  = -O2 -Wall -Wextra -std=c11 -Wno-unused-result
LDFLAGS = -lm

# Mint builds reader locally; beacon cross-compiled for Pi (arm64)
CROSS   = aarch64-linux-gnu-gcc

.PHONY: all clean beacon reader deploy-pi deploy-pi2

all: reader beacon-arm

reader: reader.c
	$(CC) $(CFLAGS) -o reader reader.c $(LDFLAGS)

beacon-arm: beacon.c
	$(CROSS) $(CFLAGS) -o beacon-arm beacon.c $(LDFLAGS)

# Build beacon natively on the Pi via SSH
beacon-pi:
	scp beacon.c pi:/tmp/beacon.c
	ssh pi "gcc -O2 -Wall -std=c11 -o /tmp/beacon /tmp/beacon.c -lm && echo ok"

beacon-pi2:
	scp beacon.c pi2:/tmp/beacon.c
	ssh pi2 "gcc -O2 -Wall -std=c11 -o /tmp/beacon /tmp/beacon.c -lm && echo ok"

deploy-pi: beacon-pi
	ssh pi "sudo cp /tmp/beacon /usr/local/bin/beacon && echo deployed"

deploy-pi2: beacon-pi2
	ssh pi2 "sudo cp /tmp/beacon /usr/local/bin/beacon && echo deployed"

deploy: deploy-pi deploy-pi2

clean:
	rm -f reader beacon-arm
