from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import uuid
import hmac
import hashlib
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

SUPABASE_URL        = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY        = os.environ.get("SUPABASE_KEY", "")
WAVE_PAYMENT_URL    = os.environ.get("WAVE_PAYMENT_URL", "")
WAVE_WEBHOOK_SECRET = os.environ.get("WAVE_WEBHOOK_SECRET", "")
MAX_PER_BUS = 36


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


@app.route("/api/reserve", methods=["POST"])
def reserve():
    data  = request.get_json()
    name  = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()

    if not name or not phone:
        return jsonify({"error": "Le nom et le numéro sont obligatoires."}), 400
    if len(phone.replace(" ", "").replace("-", "")) < 8:
        return jsonify({"error": "Numéro invalide."}), 400

    try:
        supabase = get_supabase()
        existing = supabase.table("reservations").select("id,status,token").eq("phone", phone).execute()

        for row in existing.data:
            if row["status"] == "confirmed":
                return jsonify({"error": "Ce numéro a déjà une place confirmée."}), 409
            if row["status"] in ("pending", "paid"):
                wave_url = f"{WAVE_PAYMENT_URL}?amount=1500&client_reference={row['token']}&note=Caravane+Universitaire"
                return jsonify({"token": row["token"], "wave_url": wave_url, "message": "Réservation en attente déjà existante."})

        token    = str(uuid.uuid4())
        wave_url = f"{WAVE_PAYMENT_URL}?amount=1500&client_reference={token}&note=Caravane+Universitaire"
        supabase.table("reservations").insert({"name": name, "phone": phone, "status": "pending", "token": token}).execute()
        return jsonify({"success": True, "token": token, "wave_url": wave_url})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Wave appelle automatiquement ce endpoint après chaque paiement réussi
@app.route("/api/webhook", methods=["POST"])
def wave_webhook():
    if WAVE_WEBHOOK_SECRET:
        signature  = request.headers.get("X-Wave-Signature", "")
        body_bytes = request.get_data()
        expected   = hmac.new(WAVE_WEBHOOK_SECRET.encode(), body_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return jsonify({"error": "Signature invalide"}), 401

    data        = request.get_json()
    event_type  = data.get("type", "")
    if event_type != "checkout.session.completed":
        return jsonify({"received": True}), 200

    payment     = data.get("data", {})
    token       = payment.get("client_reference", "")
    wave_status = payment.get("payment_status", "")

    if not token or wave_status != "succeeded":
        return jsonify({"received": True}), 200

    try:
        supabase    = get_supabase()
        result      = supabase.table("reservations").select("*").eq("token", token).execute()
        if not result.data or result.data[0]["status"] == "confirmed":
            return jsonify({"received": True}), 200

        count_result = supabase.table("reservations").select("id", count="exact").eq("status", "confirmed").execute()
        total        = count_result.count or 0
        bus_number   = (total // MAX_PER_BUS) + 1
        seat_number  = (total % MAX_PER_BUS) + 1

        supabase.table("reservations").update({
            "status": "confirmed", "bus_number": bus_number, "seat_number": seat_number
        }).eq("token", token).execute()

        return jsonify({"received": True, "confirmed": True}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Le bouton "J'ai payé" vérifie si Wave a bien confirmé via webhook
@app.route("/api/confirm", methods=["POST"])
def confirm():
    data  = request.get_json()
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "Token manquant."}), 400

    try:
        supabase    = get_supabase()
        result      = supabase.table("reservations").select("*").eq("token", token).execute()
        if not result.data:
            return jsonify({"error": "Réservation introuvable."}), 404

        reservation = result.data[0]

        if reservation["status"] == "confirmed":
            return jsonify({"success": True, "reservation": {
                "name": reservation["name"], "phone": reservation["phone"],
                "bus_number": reservation["bus_number"], "seat_number": reservation["seat_number"]
            }})

        # Paiement pas encore reçu de Wave
        return jsonify({
            "success": False,
            "error": "Paiement non encore reçu. Vérifiez que le paiement Wave est complété, puis réessayez dans quelques secondes."
        }), 402

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reservations", methods=["GET"])
def get_reservations():
    try:
        supabase = get_supabase()
        result   = supabase.table("reservations").select("name,phone,bus_number,seat_number").eq("status", "confirmed").order("bus_number").order("seat_number").execute()
        buses    = {}
        for r in result.data:
            bus = r["bus_number"]
            if bus not in buses:
                buses[bus] = []
            buses[bus].append({"name": r["name"], "phone": _mask_phone(r["phone"]), "seat": r["seat_number"]})
        return jsonify({"buses": [{"bus": k, "passengers": v, "count": len(v)} for k, v in sorted(buses.items())], "total": len(result.data), "max_per_bus": MAX_PER_BUS})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats", methods=["GET"])
def get_stats():
    try:
        supabase     = get_supabase()
        count_result = supabase.table("reservations").select("id", count="exact").eq("status", "confirmed").execute()
        total        = count_result.count or 0
        return jsonify({
            "total_confirmed": total, "current_bus": (total // MAX_PER_BUS) + 1,
            "seats_taken_in_bus": total % MAX_PER_BUS, "seats_left_in_bus": MAX_PER_BUS - (total % MAX_PER_BUS), "max_per_bus": MAX_PER_BUS
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _mask_phone(phone: str) -> str:
    if len(phone) <= 4: return phone
    return phone[:2] + "*" * (len(phone) - 4) + phone[-2:]


if __name__ == "__main__":
    app.run(debug=True, port=5000)
