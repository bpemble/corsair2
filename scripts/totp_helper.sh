#!/bin/bash
# Auto-enter TOTP code into IBKR Gateway's "Second Factor Authentication" dialog.
# IBC selects "Mobile Authenticator app" via SecondFactorDevice config.
# This script waits for the TOTP input dialog, clicks the text field, and types the code.
#
# Requires: xdotool, openssl, perl, TOTP_SECRET env var, DISPLAY set

set +e

if [ -z "$TOTP_SECRET" ]; then
    echo ".> TOTP helper: TOTP_SECRET not set, skipping"
    exit 0
fi

generate_totp() {
    perl -e '
        use strict; use warnings;
        my $s = uc($ARGV[0]); my $c = int(time()/30);
        my %b = map { ("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"=~/./g)[$_] => sprintf("%05b",$_) } 0..31;
        my $bits = join "", map { $b{$_}//"" } grep { $_ ne "=" } split //,$s;
        my $k = pack "B*",$bits; my $m = pack "NN",0,$c;
        my $kh = unpack "H*",$k;
        open my $f,">","/tmp/_t.bin" or die; binmode $f; print $f $m; close $f;
        my $h = `openssl dgst -sha1 -mac HMAC -macopt hexkey:$kh /tmp/_t.bin 2>/dev/null`;
        $h=~s/.*=\s*//; chomp $h; unlink "/tmp/_t.bin";
        my @b = map{hex($_)}($h=~/../g);
        my $o = $b[-1]&0x0F;
        my $r = (($b[$o]&0x7F)<<24)|($b[$o+1]<<16)|($b[$o+2]<<8)|$b[$o+3];
        printf "%06d\n",$r%1000000;
    ' "$1"
}

echo ".> TOTP helper: watching for 2FA dialog..."

# Phase 1: Wait for the 2FA dialog to appear
TIMEOUT=240
FOUND=0
while [ $TIMEOUT -gt 0 ]; do
    WIN=$(xdotool search --name "Second Factor Authentication" 2>/dev/null | head -1)
    if [ -n "$WIN" ]; then
        FOUND=1
        echo ".> TOTP helper: found initial 2FA dialog (window $WIN)"
        break
    fi
    sleep 0.5
    TIMEOUT=$((TIMEOUT - 1))
done

if [ $FOUND -eq 0 ]; then
    echo ".> TOTP helper: no 2FA dialog found within timeout"
    exit 0
fi

# Phase 2: IBC selects "Mobile Authenticator app" and clicks OK.
# This causes the dialog to close and a NEW dialog opens with the TOTP input field.
# Wait for the dialog to cycle (close + reopen).
echo ".> TOTP helper: waiting for IBC to handle device selection..."
sleep 8

# Phase 3: Find the NEW dialog (TOTP code entry)
WIN2=$(xdotool search --name "Second Factor Authentication" 2>/dev/null | tail -1)
if [ -z "$WIN2" ]; then
    echo ".> TOTP helper: no TOTP entry dialog found after device selection"
    exit 0
fi
echo ".> TOTP helper: found TOTP entry dialog (window $WIN2)"

# Get dialog geometry for absolute click positioning
GEO=$(xdotool getwindowgeometry $WIN2 2>/dev/null)
echo ".> TOTP helper: dialog geometry: $GEO"

# Extract position and size
POS_X=$(echo "$GEO" | grep Position | sed 's/.*Position: \([0-9]*\),.*/\1/')
POS_Y=$(echo "$GEO" | grep Position | sed 's/.*,\([0-9]*\) .*/\1/')
SIZE_W=$(echo "$GEO" | grep Geometry | sed 's/.*Geometry: \([0-9]*\)x.*/\1/')
SIZE_H=$(echo "$GEO" | grep Geometry | sed 's/.*x\([0-9]*\)/\1/')

echo ".> TOTP helper: dialog at ($POS_X,$POS_Y) size ${SIZE_W}x${SIZE_H}"

# Calculate text field position (center-x, ~65% down the dialog height)
CLICK_X=$((POS_X + SIZE_W / 2))
CLICK_Y=$((POS_Y + SIZE_H * 65 / 100))
echo ".> TOTP helper: clicking text field at ($CLICK_X, $CLICK_Y)"

# Phase 4: Generate TOTP code fresh
CODE=$(generate_totp "$TOTP_SECRET")
echo ".> TOTP helper: generated code $CODE"

# Phase 5: Click the text field using absolute coordinates and type
xdotool mousemove $CLICK_X $CLICK_Y 2>/dev/null
sleep 0.2
xdotool click 1 2>/dev/null
sleep 0.3
xdotool key ctrl+a 2>/dev/null
sleep 0.1
xdotool type --clearmodifiers --delay 30 "$CODE" 2>/dev/null
sleep 0.3
xdotool key Return 2>/dev/null
echo ".> TOTP helper: code $CODE submitted (attempt 1)"

# Phase 6: Retry if dialog still open after 10s
sleep 10
WIN3=$(xdotool search --name "Second Factor Authentication" 2>/dev/null | tail -1)
if [ -n "$WIN3" ]; then
    echo ".> TOTP helper: dialog still open, retrying..."
    CODE2=$(generate_totp "$TOTP_SECRET")
    echo ".> TOTP helper: retry code $CODE2"

    GEO2=$(xdotool getwindowgeometry $WIN3 2>/dev/null)
    PX=$(echo "$GEO2" | grep Position | sed 's/.*Position: \([0-9]*\),.*/\1/')
    PY=$(echo "$GEO2" | grep Position | sed 's/.*,\([0-9]*\) .*/\1/')
    SW=$(echo "$GEO2" | grep Geometry | sed 's/.*Geometry: \([0-9]*\)x.*/\1/')
    SH=$(echo "$GEO2" | grep Geometry | sed 's/.*x\([0-9]*\)/\1/')
    CX=$((PX + SW / 2))
    CY=$((PY + SH * 65 / 100))

    xdotool mousemove $CX $CY 2>/dev/null
    sleep 0.2
    xdotool click 1 2>/dev/null
    sleep 0.2
    xdotool key ctrl+a 2>/dev/null
    sleep 0.1
    xdotool type --clearmodifiers --delay 30 "$CODE2" 2>/dev/null
    sleep 0.3
    xdotool key Return 2>/dev/null
    echo ".> TOTP helper: retry code $CODE2 submitted (attempt 2)"
fi
