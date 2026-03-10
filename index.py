from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import uuid
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
WAVE_PAYMENT_URL = os.environ.get("WAVE_PAYMENT_URL", "https://pay.wave.com/m/M_sn_hfqF10djtEqb/c/sn/?amount=1500")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

MAX_PER_BUS = 36


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ─── RÉSERVER UNE PLACE ───────────────────────────────────────────────────────
@app.route("/api/reserve", methods=["POST"])
def reserve():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()

    if not name or not phone:
        return jsonify({"error": "Le nom et le numéro de téléphone sont obligatoires."}), 400

    if len(phone) < 8:
        return jsonify({"error": "Numéro de téléphone invalide."}), 400

    try:
        supabase = get_supabase()

        # Vérifier si ce numéro a déjà une réservation confirmée
        existing = (
            supabase.table("reservations")
            .select("id, status")
            .eq("phone", phone)
            .execute()
        )
        for row in existing.data:
            if row["status"] == "confirmed":
                return jsonify({"error": "Ce numéro de téléphone a déjà une place réservée et confirmée."}), 409
            if row["status"] == "pending":
                # On retourne le token existant pour qu'il puisse repayer
                token = (
                    supabase.table("reservations")
                    .select("token, name")
                    .eq("phone", phone)
                    .eq("status", "pending")
                    .execute()
                    .data[0]
                )
                wave_url = f"{WAVE_PAYMENT_URL}?amount=1500&client_reference={token['token']}&note=Caravane+Universitaire"
                return jsonify({
                    "token": token["token"],
                    "wave_url": wave_url,
                    "message": "Une réservation en attente existe déjà pour ce numéro."
                })

        token = str(uuid.uuid4())

        supabase.table("reservations").insert({
            "name": name,
            "phone": phone,
            "status": "pending",
            "token": token
        }).execute()

        wave_url = f"{WAVE_PAYMENT_URL}?amount=1500&client_reference={token}&note=Caravane+Universitaire"

        return jsonify({
            "success": True,
            "token": token,
            "wave_url": wave_url,
            "name": name,
            "phone": phone
        })

    except Exception as e:
        return jsonify({"error": f"Erreur serveur : {str(e)}"}), 500


# ─── CONFIRMER LE PAIEMENT ────────────────────────────────────────────────────
@app.route("/api/confirm", methods=["POST"])
def confirm():
    data = request.get_json()
    token = (data.get("token") or "").strip()

    if not token:
        return jsonify({"error": "Token manquant."}), 400

    try:
        supabase = get_supabase()

        # Chercher la réservation avec ce token
        result = (
            supabase.table("reservations")
            .select("*")
            .eq("token", token)
            .execute()
        )

        if not result.data:
            return jsonify({"error": "Aucune réservation trouvée avec ce token."}), 404

        reservation = result.data[0]

        if reservation["status"] == "confirmed":
            return jsonify({
                "success": True,
                "already_confirmed": True,
                "reservation": {
                    "name": reservation["name"],
                    "phone": reservation["phone"],
                    "bus_number": reservation["bus_number"],
                    "seat_number": reservation["seat_number"]
                }
            })

        # Compter les places confirmées pour assigner bus et siège
        count_result = (
            supabase.table("reservations")
            .select("id", count="exact")
            .eq("status", "confirmed")
            .execute()
        )
        total_confirmed = count_result.count or 0

        bus_number = (total_confirmed // MAX_PER_BUS) + 1
        seat_number = (total_confirmed % MAX_PER_BUS) + 1

        # Confirmer la réservation
        updated = (
            supabase.table("reservations")
            .update({
                "status": "confirmed",
                "bus_number": bus_number,
                "seat_number": seat_number
            })
            .eq("token", token)
            .execute()
        )

        r = updated.data[0]
        return jsonify({
            "success": True,
            "reservation": {
                "name": r["name"],
                "phone": r["phone"],
                "bus_number": r["bus_number"],
                "seat_number": r["seat_number"]
            }
        })

    except Exception as e:
        return jsonify({"error": f"Erreur serveur : {str(e)}"}), 500


# ─── LISTE DES INSCRITS ───────────────────────────────────────────────────────
@app.route("/api/reservations", methods=["GET"])
def get_reservations():
    try:
        supabase = get_supabase()

        result = (
            supabase.table("reservations")
            .select("name, phone, bus_number, seat_number")
            .eq("status", "confirmed")
            .order("bus_number")
            .order("seat_number")
            .execute()
        )

        buses = {}
        for r in result.data:
            bus = r["bus_number"]
            if bus not in buses:
                buses[bus] = []
            buses[bus].append({
                "name": r["name"],
                "phone": _mask_phone(r["phone"]),
                "seat": r["seat_number"]
            })

        return jsonify({
            "buses": [
                {"bus": k, "passengers": v, "count": len(v)}
                for k, v in sorted(buses.items())
            ],
            "total": len(result.data),
            "max_per_bus": MAX_PER_BUS
        })

    except Exception as e:
        return jsonify({"error": f"Erreur serveur : {str(e)}"}), 500


# ─── STATS (places disponibles) ───────────────────────────────────────────────
@app.route("/api/stats", methods=["GET"])
def get_stats():
    try:
        supabase = get_supabase()

        count_result = (
            supabase.table("reservations")
            .select("id", count="exact")
            .eq("status", "confirmed")
            .execute()
        )
        total = count_result.count or 0

        current_bus = (total // MAX_PER_BUS) + 1
        seats_taken_in_bus = total % MAX_PER_BUS
        seats_left_in_bus = MAX_PER_BUS - seats_taken_in_bus

        return jsonify({
            "total_confirmed": total,
            "current_bus": current_bus,
            "seats_taken_in_bus": seats_taken_in_bus,
            "seats_left_in_bus": seats_left_in_bus,
            "max_per_bus": MAX_PER_BUS
        })

    except Exception as e:
        return jsonify({"error": f"Erreur serveur : {str(e)}"}), 500


def _mask_phone(phone: str) -> str:
    """Masquer les chiffres du milieu pour la confidentialité."""
    if len(phone) <= 4:
        return phone
    return phone[:2] + "*" * (len(phone) - 4) + phone[-2:]


if __name__ == "__main__":
    app.run(debug=True, port=5000)