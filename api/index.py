from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import uuid
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY", "")
WAVE_PAYMENT_URL = os.environ.get("WAVE_PAYMENT_URL", "")
BASE_URL         = os.environ.get("BASE_URL", "http://localhost:5000")
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
            if row["status"] == "pending":
                success_url = f"{BASE_URL}/success.html?token={row['token']}"
                wave_url = f"{WAVE_PAYMENT_URL}?amount=1500&client_reference={row['token']}&note=Caravane+Universitaire&success_url={success_url}"
                return jsonify({"token": row["token"], "wave_url": wave_url, "message": "Réservation en attente déjà existante."})

        token       = str(uuid.uuid4())
        success_url = f"{BASE_URL}/success.html?token={token}"
        wave_url    = f"{WAVE_PAYMENT_URL}?amount=1500&client_reference={token}&note=Caravane+Universitaire&success_url={success_url}"

        supabase.table("reservations").insert({
            "name": name, "phone": phone, "status": "pending", "token": token
        }).execute()

        return jsonify({"success": True, "token": token, "wave_url": wave_url})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/confirm", methods=["POST"])
def confirm():
    data  = request.get_json()
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "Token manquant."}), 400

    try:
        supabase = get_supabase()
        result   = supabase.table("reservations").select("*").eq("token", token).execute()

        if not result.data:
            return jsonify({"error": "Réservation introuvable."}), 404

        reservation = result.data[0]

        # Déjà confirmé
        if reservation["status"] == "confirmed":
            return jsonify({"success": True, "reservation": {
                "name": reservation["name"], "phone": reservation["phone"],
                "bus_number": reservation["bus_number"], "seat_number": reservation["seat_number"]
            }})

        # Pending → Wave a redirigé ici donc paiement effectué → on confirme
        if reservation["status"] == "pending":
            count_result = supabase.table("reservations").select("id", count="exact").eq("status", "confirmed").execute()
            total        = count_result.count or 0
            bus_number   = (total // MAX_PER_BUS) + 1
            seat_number  = (total % MAX_PER_BUS) + 1

            updated = supabase.table("reservations").update({
                "status": "confirmed",
                "bus_number": bus_number,
                "seat_number": seat_number
            }).eq("token", token).execute()

            r = updated.data[0]
            return jsonify({"success": True, "reservation": {
                "name": r["name"], "phone": r["phone"],
                "bus_number": r["bus_number"], "seat_number": r["seat_number"]
            }})

        return jsonify({"success": False, "error": "Statut inconnu."}), 400

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
        return jsonify({
            "buses": [{"bus": k, "passengers": v, "count": len(v)} for k, v in sorted(buses.items())],
            "total": len(result.data), "max_per_bus": MAX_PER_BUS
        })
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
            "seats_taken_in_bus": total % MAX_PER_BUS,
            "seats_left_in_bus": MAX_PER_BUS - (total % MAX_PER_BUS),
            "max_per_bus": MAX_PER_BUS
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _mask_phone(phone: str) -> str:
    if len(phone) <= 4: return phone
    return phone[:2] + "*" * (len(phone) - 4) + phone[-2:]


if __name__ == "__main__":
    app.run(debug=True, port=5000)
